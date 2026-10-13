"""
Real-time Voice Agent with Twilio Media Streams + Sarvam WebSocket APIs
Achieves sub-1-second latency using streaming audio processing.

Architecture (OPTIMIZED):
    Twilio (mu-law 8kHz) → WebSocket → Sarvam STT → LLM (streaming) → TTS (streaming) → Twilio
    
Key optimizations:
    1. LLM streaming - start TTS while LLM is still generating
    2. Sentence-level TTS - speak first sentence immediately
    3. Connection pooling - reuse STT/TTS WebSocket connections
    4. Parallel processing - overlap LLM and TTS where possible
"""
import sys
import io

# Ensure UTF-8 output encoding across Windows consoles to avoid CP1252 charmap crashes
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form
from fastapi.responses import Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Any, Optional
import asyncio
import json
import base64
import time
import uuid
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
# Import configuration
import config
# Import modules
from modules.sarvam_tts import SarvamRealtimeTTS
from modules.sarvam_stt import TrueStreamingSTT, StreamingSTTResult, SarvamRealtimeSTT  # STT classes
from modules.audio_utils import (
    twilio_to_sarvam, 
    mp3_to_mulaw,
    pcm_to_wav,
    AudioBuffer,  
    SilenceDetector,
    audio_to_base64,
    trim_silence
)
from modules.llm_client import get_chatbot_response, get_chatbot_response_with_file_search
from modules.mongodb import init_mongodb
from modules.google_sheets import (
    save_appointment_to_mongodb_and_sheets,
    extract_appointment_with_llm
)
from modules.mongodb import (
    create_scheduled_call,
    get_scheduled_call,
    get_pending_scheduled_calls,
    get_all_scheduled_calls,
    update_scheduled_call,
    cancel_scheduled_call,
    get_upcoming_appointments,
    mark_appointment_reminder_scheduled,
    delete_old_scheduled_calls,
    MONGODB_AVAILABLE
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import re
import traceback
import random
import os

# ============================================================================
# TEXT NORMALIZATION FOR TTS - Better pronunciation of numbers, abbreviations
# ============================================================================

def normalize_text_for_tts(text: str, language: str = "ta-IN") -> str:
    """
    Normalize text for better TTS pronunciation.
    Handles: times, numbers, abbreviations, English words in Tamil context.
    
    Args:
        text: Input text to normalize
        language: Target language (ta-IN, en-IN, etc.)
    
    Returns:
        Normalized text with better pronunciation hints
    """
    if not text:
        return text
    
    normalized = text
    
    # ===== TIME NORMALIZATION =====
    # Convert "4:00" → "4 மணி" (Tamil) or "4 o'clock" (English)
    # Convert "4:30" → "4 மணி 30 நிமிடம்" (Tamil)
    
    def time_to_spoken(match):
        hour = match.group(1)
        minute = match.group(2)
        period = match.group(3).strip().upper() if match.group(3) else ""
        
        # Convert period to Tamil
        if language == "ta-IN":
            period_tamil = ""
            if period == "AM":
                period_tamil = "காலை"
            elif period == "PM":
                # Determine if it's afternoon or evening/night based on hour
                hour_int = int(hour)
                if hour_int < 5 or hour_int == 12:
                    period_tamil = "மதியம்"
                elif hour_int < 8:
                    period_tamil = "மாலை"
                else:
                    period_tamil = "இரவு"
            
            if minute and minute != "00":
                return f"{period_tamil} {hour} மணி {int(minute)} நிமிடம்".strip()
            else:
                return f"{period_tamil} {hour} மணி".strip()
        else:  # English
            if minute and minute != "00":
                return f"{hour} {minute} {period}".strip()
            else:
                return f"{hour} o'clock {period}".strip()
    
    # Match time patterns like 4:00, 10:30 AM, 3:00 PM
    normalized = re.sub(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm|A\.M\.|P\.M\.)?', time_to_spoken, normalized)
    
    # ===== ABBREVIATION EXPANSION =====
    # Common hospital/medical abbreviations - expand to spelled out form
    if language == "ta-IN":
        abbreviations_tamil = {
            # Hospital name and departments
            'Anurag': 'அனுராக்',
            'RK': 'ஆர் கே',
            'ENT': 'இ என் டி',
            'OPD': 'ஓ பி டி',
            'ICU': 'ஐ சி யூ',
            'ECG': 'இ சி ஜி',
            'MRI': 'எம் ஆர் ஐ',
            'CT': 'சி டி',
            'BP': 'பி பி',
            'IV': 'ஐ வி',
            'OP': 'ஓ பி',
            'IP': 'ஐ பி',
            
            # Doctor titles
            'Dr': 'டாக்டர்',
            
            # Common abbreviations
            'OK': 'ஓகே',
            'SMS': 'எஸ் எம் எஸ்',
            'ID': 'ஐ டி',
            'PM': 'பி எம்',
            'AM': 'ஏ எம்',
        }
        
        for abbr, expansion in abbreviations_tamil.items():
            # Match whole word only (case insensitive for some)
            normalized = re.sub(r'\b' + abbr + r'\b', expansion, normalized)
    else:
        # English - just add spaces for abbreviations
        abbreviations_english = {
            'ENT': 'E N T',
            'OPD': 'O P D',
            'ICU': 'I C U',
            'ECG': 'E C G',
            'MRI': 'M R I',
            'CT': 'C T',
            'BP': 'B P',
            'Dr': 'Doctor',
        }
        
        for abbr, expansion in abbreviations_english.items():
            normalized = re.sub(r'\b' + abbr + r'\b', expansion, normalized)
    
    # ===== PHONE NUMBER SPACING =====
    # Add pauses in phone numbers for clarity: 044-2626-XXXX
    def format_phone(match):
        return ' '.join(match.group(0))
    
    # Match Indian phone patterns
    # normalized = re.sub(r'\b(\d{3,5}[-\s]?\d{3,4}[-\s]?\d{3,4})\b', format_phone, normalized)
    
    # ===== CLEAN UP =====
    # Remove multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized


# ============================================================================
# RAG TRIGGER DETECTION - Determine if query needs knowledge base
# ============================================================================

# Keywords that indicate a query needs external knowledge
RAG_TRIGGER_KEYWORDS = {
    # English keywords
    'en': [
        # Pricing related
        'cost', 'price', 'fee', 'charge', 'rate', 'how much', 'payment', 'insurance',
        'consultation fee', 'test cost', 'room charge', 'bill', 'expense',
        # Doctor/Schedule related
        'available', 'timing', 'schedule', 'when is', 'which doctor', 'specialist',
        'dr.', 'doctor available', 'appointment time', 'open', 'closed', 'working hours',
        # Services/Facilities
        'services', 'facilities', 'departments', 'tests', 'scan', 'x-ray', 'mri', 'ct',
        'blood test', 'ecg', 'ultrasound', 'lab', 'pharmacy', 'ambulance',
        # Hospital info
        'address', 'location', 'where', 'parking', 'directions', 'landmark',
        # Policies
        'policy', 'rules', 'visiting hours', 'admission', 'discharge',
    ],
    # Tamil keywords (native + transliterated/Tanglish)
    'ta': [
        # Pricing - Tamil native script
        'எவ்வளவு', 'விலை', 'கட்டணம்', 'செலவு', 'ஃபீஸ்', 'பணம்', 'ரேட்',
        'எவ்ளோ', 'என்ன விலை', 'என்ன கட்டணம்',
        # Pricing - Transliterated/Tanglish (commonly used)
        'சார்ஜ்', 'சார்ஜஸ்', 'பிரைஸ்', 'பிரைசிங்', 'காஸ்ட்', 'ஃபீ',
        'charge', 'charges', 'pricing', 'fees', 'evlo', 'evalavu',
        # Doctor/Schedule - Tamil native + transliterated
        'டாக்டர்', 'மருத்துவர்', 'எப்போ', 'நேரம்', 'available', 'இருக்காங்களா',
        'எந்த டாக்டர்', 'யார் இருக்காங்க', 'timing', 'schedule',
        'டைமிங்', 'ஷெட்யூல்', 'ஷெட்யூல்ஸ்',  # Transliterated timing/schedule
        'அவைலபிலிட்டி', 'அவைலபில்',  # Transliterated availability
        'செக்கப்', 'செக்-அப்', 'checkup', 'check up', 'check-up',
        # Services - Tamil
        'டெஸ்ட்', 'ஸ்கேன்', 'எக்ஸ்ரே', 'ரத்த பரிசோதனை', 'lab', 'pharmacy',
        'மருந்து', 'அம்புலன்ஸ்', 'ஜெனரல்', 'general',
        # Hospital info - Tamil
        'எங்க', 'address', 'location', 'பார்க்கிங்',
    ]
}

def should_use_rag(query: str, language: str = 'en-IN') -> bool:
    """
    Determine if a query should trigger RAG (File Search).
    
    Args:
        query: User's question/message
        language: User's language code
    
    Returns:
        True if RAG should be used, False otherwise
    
    Logic:
    - RAG for: pricing, timing, doctor info, services, policies
    - No RAG for: greetings, name, booking flow, confirmations
    """
    query_lower = query.lower()
    
    # Get language key
    lang_key = 'ta' if 'ta' in language else 'en'
    
    # Check for trigger keywords
    keywords = RAG_TRIGGER_KEYWORDS.get(lang_key, []) + RAG_TRIGGER_KEYWORDS.get('en', [])
    
    for keyword in keywords:
        if keyword.lower() in query_lower:
            print(f"🔍 RAG triggered by keyword: '{keyword}'")
            return True
    
    # Check for question patterns that likely need external info
    question_patterns = [
        r'எவ்வளவு',  # How much (Tamil)
        r'என்ன (விலை|கட்டணம்|நேரம்)',  # What (price/fee/time)
        r'எப்போ',  # When (Tamil)
        r'எந்த',  # Which (Tamil)
        r'how much',
        r'what is the (cost|fee|price|charge)',
        r'when (is|does|can)',
        r'which doctor',
        r'is .* available',
        r'do you have',
    ]
    
    for pattern in question_patterns:
        if re.search(pattern, query_lower):
            print(f"🔍 RAG triggered by pattern: '{pattern}'")
            return True
    
    return False


# ============================================================================
# BACKGROUND AMBIENT AUDIO - Keeps call feeling alive during processing
# ============================================================================

BACKGROUND_AUDIO_CACHE = []  # Pre-loaded ambient audio chunks (base64 mulaw)
BACKGROUND_AUDIO_LOADED = False

def load_background_audio():
    """
    Load and convert background ambient audio to mulaw chunks at startup.
    Audio is reduced to -12dB so it's audible but won't overpower speech.
    """
    global BACKGROUND_AUDIO_CACHE, BACKGROUND_AUDIO_LOADED
    
    try:
        from pydub import AudioSegment
        
        audio_path = os.path.join(os.path.dirname(__file__), "asset", "Office Background Ambience _ Free Sound Effect-[AudioTrimmer.com].mp3")
        
        if not os.path.exists(audio_path):
            print(f"⚠️ Background audio not found: {audio_path}")
            return
        
        print("🎵 Loading background ambient audio...")
        
        # Load MP3
        try:
            audio = AudioSegment.from_mp3(audio_path)
        except Exception as audio_err:
            print(f"⚠️ Note: Background audio not loaded (ffmpeg or decoder unavailable: {audio_err}). Audio will stream normally without background ambience.")
            return
        
        # Convert to Twilio format: mono, 8kHz
        audio = audio.set_channels(1)
        audio = audio.set_frame_rate(8000)
        audio = audio.set_sample_width(2)  # 16-bit
        
        # Reduce volume to -6dB - clearly audible ambient sound
        audio = audio - 1
        
        # Get raw PCM
        pcm_data = audio.raw_data
        
        # Convert to mu-law
        try:
            import audioop
        except ImportError:
            import audioop_lts as audioop
        
        mulaw_data = audioop.lin2ulaw(pcm_data, 2)
        
        # Split into 20ms chunks (160 bytes at 8kHz mulaw)
        chunk_size = 160  # 8000 Hz * 0.02s = 160 samples
        chunks = []
        for i in range(0, len(mulaw_data), chunk_size):
            chunk = mulaw_data[i:i + chunk_size]
            if len(chunk) == chunk_size:  # Only full chunks
                chunks.append(base64.b64encode(chunk).decode())
        
        BACKGROUND_AUDIO_CACHE = chunks
        BACKGROUND_AUDIO_LOADED = True
        
        duration_sec = len(mulaw_data) / 8000
        print(f"✅ Background audio loaded: {len(chunks)} chunks, {duration_sec:.1f}s duration, -12dB volume")
        
    except ImportError:
        print("⚠️ pydub not installed - background audio disabled. Run: pip install pydub")
    except Exception as e:
        print(f"❌ Failed to load background audio: {e}")
        import traceback
        traceback.print_exc()


async def start_background_audio_loop(websocket, stream_sid: str, session: dict):
    """
    Continuously stream background ambient audio throughout the ENTIRE call.
    NO PAUSING - plays from call pickup to call end.
    Loops the audio file seamlessly.
    """
    global BACKGROUND_AUDIO_CACHE
    
    if not BACKGROUND_AUDIO_CACHE:
        print("⚠️ Background audio cache empty - skipping")
        return
    
    print("🎵 Starting continuous background audio (no pausing)...")
    
    chunk_index = 0
    chunk_interval = 0.02  # 20ms per chunk
    chunks_sent = 0
    
    try:
        while not session.get("call_ended", False):
            # NO PAUSING - audio plays continuously throughout the call
            
            # Get current chunk
            chunk_b64 = BACKGROUND_AUDIO_CACHE[chunk_index]
            
            # Send to Twilio
            try:
                await websocket.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": chunk_b64}
                })
                chunks_sent += 1
                
                # Log every 500 chunks (~10 seconds)
                if chunks_sent % 500 == 0:
                    print(f"🎵 Background audio playing: {chunks_sent} chunks sent ({chunks_sent * 0.02:.0f}s)")
                    
            except Exception as e:
                print(f"⚠️ Background audio send error: {e}")
                break
            
            # Move to next chunk (loop back to start when done)
            chunk_index = (chunk_index + 1) % len(BACKGROUND_AUDIO_CACHE)
            
            # Wait 20ms before next chunk
            await asyncio.sleep(chunk_interval)
            
    except asyncio.CancelledError:
        print(f"🎵 Background audio stopped after {chunks_sent} chunks ({chunks_sent * 0.02:.0f}s)")
    except Exception as e:
        print(f"⚠️ Background audio loop error: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"🎵 Background audio ended - total: {chunks_sent} chunks")


# Pre-load background audio at module import
load_background_audio()


# ============================================================================
# COMPREHENSIVE INDIAN LANGUAGE SUPPORT (English, Tamil, Telugu, Hindi)
# ============================================================================

# Goodbye keywords - Native script + Romanized (Tanglish/Hinglish/Tenglish)
GOODBYE_KEYWORDS = {
    'en': [
        # Standard English
        'bye', 'goodbye', 'good bye', 'thank you', 'thanks', 'thats all', "that's all", 
        'im done', "i'm done", 'end call', 'no thanks', 'nothing else', 'no thank you',
        'thats it', "that's it", 'ok bye', 'okay bye', 'alright bye', 'fine bye',
        'no need', 'nothing more', 'all done', 'we are done', 'hang up',
        # Mixed language (common in India)
        'illa thank', 'illa thanks', 'nahi thanks', 'vaddu thanks', 'no nandri',
        'illa bye', 'nahi bye', 'vaddu bye', 'ok nandri', 'ok dhanyawad', 'ok thanks'
    ],
    'ta': [
        # Tamil script - Goodbye/Thanks
        'நன்றி', 'மிக்க நன்றி', 'ரொம்ப நன்றி', 'தாங்க்ஸ்', 'தேங்க்யூ',
        'போயிட்டு வரேன்', 'சரி போயிட்டு வரேன்', 'போறேன்', 'வருகிறேன்',
        'பை', 'பை பை', 'குட் பை',
        # Tamil script - No/Decline
        'இல்ல', 'இல்லை', 'இல்ல நன்றி', 'இல்லை நன்றி', 'வேண்டாம்', 'வேணாம்',
        'வேண்டாம் நன்றி', 'வேணாம் நன்றி', 'போதும்', 'சரி போதும்',
        # Tamil script - Done/Finished
        'முடிஞ்சது', 'ஓகே', 'சரி', 'அவ்ளோதான்', 'இது போதும்',
        # Romanized Tamil (Tanglish) - very common
        'nandri', 'romba nandri', 'mikka nandri', 'thanks nandri',
        'poyittu varen', 'sari poyittu varen', 'poren', 'varugiren',
        'illa', 'illai', 'illa nandri', 'illai nandri', 'vendam', 'venam',
        'vendam nandri', 'venam nandri', 'podhum', 'sari podhum',
        'mudinjichu', 'mudinjadhu', 'okay', 'sari', 'avlodhan', 'idhu podhum'
    ],
    'hi': [
        # Hindi script - Goodbye/Thanks
        'धन्यवाद', 'बहुत धन्यवाद', 'शुक्रिया', 'बहुत शुक्रिया', 'थैंक्स', 'थैंक यू',
        'अलविदा', 'फिर मिलेंगे', 'चलता हूं', 'चलती हूं', 'जाता हूं', 'जाती हूं',
        'बाय', 'बाय बाय', 'गुड बाय',
        # Hindi script - No/Decline  
        'नहीं', 'नही', 'नहीं चाहिए', 'नहीं धन्यवाद', 'नहीं शुक्रिया',
        'बस', 'बस इतना ही', 'बस हो गया', 'काफी है', 'रहने दो', 'रहने दीजिए',
        # Hindi script - Done/Finished
        'हो गया', 'खत्म', 'पूरा हो गया', 'ठीक है', 'अच्छा', 'चलो',
        # Romanized Hindi (Hinglish) - very common
        'dhanyawad', 'dhanyavaad', 'bahut dhanyawad', 'shukriya', 'bahut shukriya',
        'alvida', 'phir milenge', 'chalta hun', 'chalti hun', 'jaata hun', 'jaati hun',
        'nahi', 'nahin', 'nahi chahiye', 'nahi dhanyawad', 'nahi shukriya',
        'bas', 'bas itna hi', 'bas ho gaya', 'kaafi hai', 'rehne do', 'rehne dijiye',
        'ho gaya', 'khatam', 'poora ho gaya', 'theek hai', 'accha', 'chalo'
    ],
    'te': [
        # Telugu script - Goodbye/Thanks
        'ధన్యవాదాలు', 'చాలా ధన్యవాదాలు', 'థాంక్స్', 'థాంక్ యూ',
        'వెళ్తాను', 'వెళ్ళిపోతాను', 'పోతాను', 'వస్తాను',
        'బై', 'బై బై', 'గుడ్ బై',
        # Telugu script - No/Decline
        'వద్దు', 'లేదు', 'వద్దు ధన్యవాదాలు', 'లేదు ధన్యవాదాలు', 'అక్కర్లేదు',
        'చాలు', 'ఇంతే', 'ఇంతే చాలు', 'సరిపోయింది',
        # Telugu script - Done/Finished  
        'అయిపోయింది', 'సరే', 'ఓకే', 'అంతే',
        # Romanized Telugu (Tenglish) - common
        'dhanyavadalu', 'chala dhanyavadalu', 'thanks', 'thank you',
        'veltanu', 'vellipothanu', 'pothanu', 'vasthanu',
        'vaddu', 'ledu', 'vaddu dhanyavadalu', 'ledu dhanyavadalu', 'akkarleedu',
        'chaalu', 'inthe', 'inthe chaalu', 'saripoyindi',
        'aipoyindi', 'sare', 'okay', 'anthe'
    ]
}

# Language switch request keywords - explicit language change requests
LANGUAGE_SWITCH_KEYWORDS = {
    'ta-IN': [
        # Tamil script
        'தமிழ்', 'தமிழ்ல', 'தமிழில்', 'தமிழ்ல பேசு', 'தமிழ்ல பேசுங்க', 'தமிழ்ல சொல்லுங்க',
        # Romanized
        'tamil', 'tamizh', 'thamizh', 'tamil la', 'tamizh la', 'tamilil',
        'speak tamil', 'talk tamil', 'in tamil', 'tamil please', 'tamil lo pesu',
        'tamil la pesunga', 'tamil la sollunga', 'can you speak tamil'
    ],
    'hi-IN': [
        # Hindi script
        'हिंदी', 'हिन्दी', 'हिंदी में', 'हिंदी में बोलो', 'हिंदी में बोलिए', 'हिंदी में बताओ',
        # Romanized
        'hindi', 'hindi mein', 'hindi me', 'hindi mei',
        'speak hindi', 'talk hindi', 'in hindi', 'hindi please', 'hindi mein bolo',
        'hindi mein boliye', 'hindi mein batao', 'can you speak hindi'
    ],
    'te-IN': [
        # Telugu script
        'తెలుగు', 'తెలుగులో', 'తెలుగులో చెప్పు', 'తెలుగులో మాట్లాడు', 'తెలుగులో చెప్పండి',
        # Romanized
        'telugu', 'telugu lo', 'telugulo',
        'speak telugu', 'talk telugu', 'in telugu', 'telugu please',
        'telugu lo cheppu', 'telugu lo maatlaadu', 'telugu lo cheppandi', 'can you speak telugu'
    ],
    'en-IN': [
        # From Tamil
        'இங்கிலீஷ்', 'ஆங்கிலம்', 'ஆங்கிலத்தில்', 'இங்கிலீஷ்ல பேசு',
        # From Hindi  
        'अंग्रेजी', 'इंग्लिश', 'अंग्रेजी में', 'इंग्लिश में बोलो',
        # From Telugu
        'ఇంగ్లీష్', 'ఆంగ్లం', 'ఇంగ్లీష్ లో',
        # Romanized
        'english', 'inglish', 'angrezhi', 'angrezi',
        'speak english', 'talk english', 'in english', 'english please',
        'english la pesu', 'english mein bolo', 'english lo cheppu', 'can you speak english'
    ]
}

# Booking confirmation keywords - when agent confirms booking
BOOKING_CONFIRMED_KEYWORDS = {
    'en': [
        'booked', 'confirmed', 'appointment is booked', 'appointment confirmed', 
        'appointment has been booked', 'successfully booked', 'booking confirmed',
        'see you', 'we will see you', 'looking forward', 'scheduled',
        'have you scheduled', 'i have scheduled', 'appointment at', 'appointment for',
        'your appointment', 'you are scheduled', 'we have scheduled'
    ],
    'ta': [
        # Tamil script
        'புக் ஆகிவிட்டது', 'புக் ஆகிடுச்சு', 'உறுதி செய்யப்பட்டது', 'உறுதிப்படுத்தப்பட்டது',
        'அப்பாயிண்ட்மென்ட் புக்', 'புக்கிங் முடிந்தது', 'பதிவு செய்யப்பட்டது',
        'மணிக்கு', 'அப்பாயின்ட்மெண்ட்',  # Time mention indicates booking
        # Romanized
        'book aagividtadhu', 'book aagiduchu', 'urudhi seyyapattathu',
        'appointment book', 'booking mudinjadhu', 'padhivu seyyapattathu'
    ],
    'hi': [
        # Hindi script
        'बुक हो गया', 'बुक हो गयी', 'बुकिंग हो गई', 'कन्फर्म हो गया', 'पक्का हो गया',
        'अपॉइंटमेंट बुक', 'अपॉइंटमेंट कन्फर्म', 'बुकिंग कन्फर्म',
        # Romanized
        'book ho gaya', 'book ho gayi', 'booking ho gayi', 'confirm ho gaya', 'pakka ho gaya',
        'appointment book', 'appointment confirm', 'booking confirm'
    ],
    'te': [
        # Telugu script
        'బుక్ అయింది', 'బుక్ అయిపోయింది', 'కన్ఫర్మ్ అయింది', 'అపాయింట్మెంట్ బుక్',
        'బుకింగ్ అయిపోయింది', 'నిర్ధారించబడింది',
        # Romanized
        'book ayindi', 'book aipoyindi', 'confirm ayindi', 'appointment book',
        'booking aipoyindi', 'nirdharinchbadindi'
    ]
}

def detect_goodbye(text: str, language: str = 'en') -> bool:
    """Detect if user is saying goodbye - checks ALL 4 languages for robustness.
    Handles native script, romanized (Tanglish/Hinglish/Tenglish), and mixed phrases.
    Uses word boundary matching to avoid false positives.
    
    IMPORTANT: Only triggers on CLEAR goodbye phrases, not contextual rejections like
    'I don't want today' (இன்னைக்கு வேணாம்) which is NOT a goodbye.
    """
    if not text:
        return False
    
    text_lower = text.lower().strip()
    
    # Skip if text looks like a question (not a goodbye)
    question_indicators = ['what', 'how', 'when', 'where', 'why', 'who', 'which', 
                          'can you', 'do you', 'could you', 'would you', 
                          'is there', 'are there', 'tell me', '?']
    if any(q in text_lower for q in question_indicators):
        return False
    
    # Skip if text contains scheduling/time context - NOT a goodbye
    # e.g., "இன்னைக்கு வேணாம் நாளைக்கு" (not today, tomorrow) is NOT goodbye
    scheduling_context = [
        # English
        'tomorrow', 'today', 'morning', 'evening', 'afternoon', 'night',
        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
        'o\'clock', 'am', 'pm', 'appointment', 'book', 'schedule', 'time', 'date',
        # Tamil
        'நாளைக்கு', 'நாளை', 'இன்னைக்கு', 'இன்று', 'காலை', 'மாலை', 'இரவு',
        'மணி', 'மணிக்கு', 'நேரம்', 'அப்பாயின்ட்மென்ட்', 'புக்',
        'naalai', 'naalaiku', 'innaikku', 'inru', 'kaalai', 'maalai',
        # Hindi  
        'कल', 'आज', 'सुबह', 'शाम', 'रात', 'बजे', 'समय',
        'kal', 'aaj', 'subah', 'shaam', 'raat', 'baje',
        # Telugu
        'రేపు', 'ఈరోజు', 'ఉదయం', 'సాయంత్రం', 'రాత్రి',
        'repu', 'eeroju', 'udayam', 'saayantram'
    ]
    if any(ctx in text_lower for ctx in scheduling_context):
        return False
    
    # Skip if text has rejection + alternative pattern (not goodbye)
    # e.g., "not today but tomorrow", "వేణాం కానీ నాలై" etc.
    continuation_words = [
        'but', 'instead', 'rather', 'actually', 'want', 'need', 'prefer',
        'கானி', 'ஆனால்', 'ஆனா', 'but', 'मगर', 'लेकिन', 'पर', 'కానీ', 'కాని'
    ]
    if any(cont in text_lower for cont in continuation_words):
        return False
    
    # Only match CLEAR goodbye phrases (complete farewells)
    clear_goodbye_phrases = [
        # English - must be clear goodbye, not just "thanks"
        'ok bye', 'okay bye', 'good bye', 'goodbye', 'bye bye', 'bye now',
        'thats all', "that's all", 'thats it', "that's it", 'im done', "i'm done",
        'all done', 'we are done', 'nothing else', 'nothing more', 'no more questions',
        # Tamil phrases - clear farewells only
        'போயிட்டு வரேன்', 'சரி போயிட்டு வரேன்', 'போறேன்',
        'poyittu varen', 'sari poyittu varen', 'poren',
        'சரி போதும் நன்றி', 'போதும் நன்றி', 'சரி பை', 'ஓகே பை',
        'sari podhum nandri', 'podhum nandri', 'sari bye', 'okay bye',
        # Hindi phrases - clear farewells only
        'फिर मिलेंगे', 'चलता हूं', 'चलती हूं', 'अलविदा',
        'phir milenge', 'chalta hun', 'chalti hun', 'alvida',
        'ठीक है बाय', 'अच्छा बाय', 'theek hai bye', 'accha bye',
        # Telugu phrases - clear farewells only
        'వెళ్తాను', 'వెళ్ళిపోతాను', 'veltanu', 'vellipothanu',
        'సరే బై', 'ఓకే బై', 'sare bye', 'okay bye'
    ]
    
    # Check clear goodbye phrases
    for phrase in clear_goodbye_phrases:
        if phrase in text_lower:
            return True
    
    # Single words - only VERY clear goodbye words (bye variants)
    # Removed: thanks, nandri, vendam, etc. as they can be used in context
    clear_bye_words = ['bye', 'goodbye', 'alvida', 'அலவிடா', 'अलविदा']
    
    for keyword in clear_bye_words:
        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
        if re.search(pattern, text_lower):
            return True
    
    return False


def detect_language_switch_request(text: str) -> str:
    """Detect if user is explicitly requesting to switch language. Returns language code or None."""
    if not text:
        return None
    
    text_lower = text.lower().strip()
    
    # Negative indicators - phrases that indicate user DOESN'T want a language
    negative_patterns = [
        "don't know", "dont know", "don't speak", "dont speak", 
        "don't understand", "dont understand", "can't speak", "cant speak",
        "i don't know", "i dont know", "not understand"
    ]
    
    # Positive indicators - phrases that indicate user WANTS a language  
    positive_patterns = [
        "switch to", "change to", "speak in", "talk in",
        "use ", "want ", "can you speak", "please speak", 
        "switch ", "change ", "in ", "talk ", "speak "
    ]
    
    # Find all language mentions with their positions
    lang_mentions = []  # List of (lang_code, keyword, position, is_positive, is_negative)
    
    # Keywords in native scripts are inherently positive (user is speaking that language)
    native_script_keywords = {
        'ta-IN': ['தமிழ்', 'தமிழ்ல', 'தமிழில்'],
        'hi-IN': ['हिंदी', 'हिन्दी', 'हिंदी में'],
        'te-IN': ['తెలుగు', 'తెలుగులో'],
    }
    
    for lang_code, keywords in LANGUAGE_SWITCH_KEYWORDS.items():
        for kw in keywords:
            pos = text_lower.find(kw)
            while pos != -1:
                # Check context before keyword (30 chars)
                context_before = text_lower[max(0, pos - 30):pos]
                # Check IMMEDIATE context before (10 chars) for negatives
                immediate_before = text_lower[max(0, pos - 15):pos]
                
                # Check if there's a positive indicator before this keyword
                is_positive = any(p in context_before for p in positive_patterns)
                
                # Native script keywords are inherently positive (user is speaking the language)
                if lang_code in native_script_keywords and kw in native_script_keywords[lang_code]:
                    is_positive = True
                
                # Check if there's a negative indicator IMMEDIATELY before this keyword
                # (e.g., "I don't know Tamil" - negative is right before Tamil)
                is_negative = any(n in immediate_before for n in negative_patterns)
                
                lang_mentions.append((lang_code, kw, pos, is_positive, is_negative))
                
                # Find next occurrence
                pos = text_lower.find(kw, pos + 1)
    
    # Sort by position in text
    lang_mentions.sort(key=lambda x: x[2])
    
    # Find the best match: positive mention that isn't negative
    positive_candidates = []
    negative_langs = set()
    
    for lang_code, kw, pos, is_positive, is_negative in lang_mentions:
        if is_negative:
            negative_langs.add(lang_code)
        elif is_positive and lang_code not in negative_langs:
            positive_candidates.append((lang_code, len(kw)))
    
    # If we have positive candidates, return the best one (longest keyword match)
    if positive_candidates:
        positive_candidates.sort(key=lambda x: x[1], reverse=True)
        return positive_candidates[0][0]
    
    # Fallback: if no positive indicators but language mentioned without negative
    for lang_code, kw, pos, is_positive, is_negative in lang_mentions:
        if not is_negative and lang_code not in negative_langs:
            return lang_code
    
    return None


def detect_booking_confirmed(text: str, language: str = 'en') -> bool:
    """Detect if the assistant has confirmed a booking."""
    if not text:
        return False
    
    text_lower = text.lower().strip()
    
    # Check language-specific keywords
    lang_code = language.split('-')[0] if '-' in language else language
    keywords = BOOKING_CONFIRMED_KEYWORDS.get(lang_code, BOOKING_CONFIRMED_KEYWORDS['en'])
    
    # Also check English keywords for all languages
    all_keywords = keywords + BOOKING_CONFIRMED_KEYWORDS['en']
    
    return any(keyword in text_lower for keyword in all_keywords)


def should_end_call_after_response(response: str, user_text: str, session: dict) -> bool:
    """
    Determine if call should end after this response.
    LANGUAGE: Only Tamil and English supported.
    KEY: End when user says thank you/bye OR agent gives farewell.
    """
    user_lower = user_text.lower().strip()
    response_lower = response.lower()
    
    # If farewell already given, END immediately on any user response
    if session.get('farewell_given', False):
        print(f"📞 Farewell already given - ending call now")
        return True
    
    # Check if this is a booking confirmation response
    if detect_booking_confirmed(response, session.get('language', 'ta-IN')):
        session['booking_confirmed'] = True
        print("📋 Booking confirmed - agent should ask if user has other questions")
        return False
    
    # Agent farewell phrases - STRONG end indicators
    agent_farewell_phrases = [
        # English
        'thank you for calling', 'thanks for calling', 'have a great day',
        'take care', 'get well soon', 'goodbye',
        # Tamil (native script)
        'அழைப்புக்கு நன்றி',  # Thanks for calling
        'சீக்கிரம் குணமாகுங்கள்',  # Get well soon
        'நல்ல நாள்',  # Good day
    ]
    
    # Check if agent gave farewell in THIS response
    if any(phrase in response_lower for phrase in agent_farewell_phrases):
        session['farewell_given'] = True
        print(f"📞 Agent gave farewell - marking for call end")
        return True
    
    # Check if agent asked "any other questions?"
    any_other_questions_phrases = [
        'வேறு ஏதாவது', 'வேறு உதவி', 'anything else', 'any other', 'is there anything'
    ]
    if any(phrase in response_lower for phrase in any_other_questions_phrases):
        session['asked_other_questions'] = True
        print("❓ Agent asked about other questions - waiting for user response")
        return False
    
    # User farewell keywords - STRONG indicators only (not common words)
    user_farewell_keywords = [
        # English - strong farewell indicators
        'no thank you', 'no thanks', 'thank you bye', 'thanks bye',
        'nothing else', 'no more', "that's all", 'done for now',
        # Tamil (native script) - strong farewell phrases only
        'நன்றி போதும்', 'போதும் நன்றி', 'அவ்வளவுதான்', 'வேறு ஒன்றும் இல்ல',
        # Romanized Tamil - strong farewells only
        'nandri podhum', 'podhum nandri', 'ok bye', 'bye bye', 'sari bye'
    ]
    
    # If agent asked "any other questions" and user responds with farewell -> END
    # But NOT if user is asking a question
    if session.get('asked_other_questions', False):
        # Don't end if user is asking a question (has ?, கேட் meaning hear/ask, என்ன meaning what)
        is_question = '?' in user_text or 'கேட்' in user_text or 'என்ன' in user_text or 'எப்போ' in user_text
        if not is_question and any(kw in user_lower for kw in user_farewell_keywords):
            print(f"📞 User said '{user_text}' after agent asked - ending call")
            return True
        # Only SHORT "no" responses (< 15 chars) without questions = farewell
        short_no_phrases = ['no', 'no no', 'இல்ல', 'இல்ல இல்ல', 'வேண்டாம்', 'illa', 'vendaam', 'nothing']
        if not is_question and user_lower.strip() in short_no_phrases and len(user_text) < 15:
            print(f"📞 User said short negative '{user_text}' after agent asked - ending call")
            return True
    
    # Explicit goodbye from user - always end
    explicit_goodbye = [
        'bye', 'goodbye', 'bye bye', 'ok bye', 'thank you bye',
        'call cut', 'cut the call', 'end call', 'disconnect', 'podhum bye'
    ]
    if any(kw in user_lower for kw in explicit_goodbye):
        print(f"📞 User said explicit goodbye - ending call")
        return True
    
    return False


def detect_script_language(text: str) -> tuple[str, int]:
    """
    Detect language from script characters in text.
    Returns (language_code, char_count) or (None, 0) if no Indian script detected.
    """
    if not text:
        return None, 0
    
    # Count characters in different scripts
    script_counts = {
        'ta-IN': len(re.findall(r'[\u0B80-\u0BFF]', text)),  # Tamil
        'hi-IN': len(re.findall(r'[\u0900-\u097F]', text)),  # Hindi/Devanagari
        'te-IN': len(re.findall(r'[\u0C00-\u0C7F]', text)),  # Telugu
        'kn-IN': len(re.findall(r'[\u0C80-\u0CFF]', text)),  # Kannada
        'ml-IN': len(re.findall(r'[\u0D00-\u0D7F]', text)),  # Malayalam
        'bn-IN': len(re.findall(r'[\u0980-\u09FF]', text)),  # Bengali
        'pa-IN': len(re.findall(r'[\u0A00-\u0A7F]', text)),  # Punjabi/Gurmukhi
        'mr-IN': len(re.findall(r'[\u0900-\u097F]', text)),  # Marathi (uses Devanagari)
        'gu-IN': len(re.findall(r'[\u0A80-\u0AFF]', text)),  # Gujarati
        'or-IN': len(re.findall(r'[\u0B00-\u0B7F]', text)),  # Odia
    }
    
    max_script = max(script_counts, key=script_counts.get)
    max_count = script_counts[max_script]
    
    if max_count > 0:
        return max_script, max_count
    return None, 0


def detect_response_language(text: str) -> str:
    """
    Detect language from text content for correct TTS voice.
    Uses character ranges to identify Indian scripts.
    """
    if not text:
        return "en-IN"
    
    script_lang, char_count = detect_script_language(text)
    
    # If significant Indian script content (>5 chars), use that language
    if script_lang and char_count > 5:
        return script_lang
    
    return "en-IN"


# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Store reminder call data for active calls (keyed by call_sid)
reminder_call_data: Dict[str, Dict] = {}


async def execute_due_scheduled_calls():
    """
    Background job: Execute all due scheduled calls.
    Called by APScheduler every minute.
    """
    global reminder_call_data
    
    try:
        pending_calls = get_pending_scheduled_calls()
        
        if not pending_calls:
            return
        
        print(f"⏰ Found {len(pending_calls)} due scheduled calls")
        
        for call in pending_calls:
            schedule_id = call.get("id")
            phone_number = call.get("phone_number")
            call_type = call.get("call_type", "manual")
            
            try:
                print(f"📞 Executing scheduled call {schedule_id} to {phone_number} (type: {call_type})")
                
                # Update status to in-progress
                update_scheduled_call(schedule_id, {"status": "in-progress"})
                
                # Trigger the call via Twilio
                from twilio.rest import Client
                twilio_client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
                
                # Use reminder endpoint for reminder calls
                if call_type == "reminder":
                    webhook_url = f"https://{config.NGROK_URL}/reminder_voice/{schedule_id}"
                else:
                    webhook_url = f"https://{config.NGROK_URL}/outbound_voice"
                
                twilio_call = twilio_client.calls.create(
                    to=phone_number,
                    from_=config.TWILIO_PHONE_NUMBER,
                    url=webhook_url,
                    status_callback=f"https://{config.NGROK_URL}/scheduled_call_status/{schedule_id}",
                    status_callback_event=["completed", "no-answer", "busy", "failed"]
                )
                
                # Store reminder data for this call
                if call_type == "reminder":
                    reminder_call_data[twilio_call.sid] = {
                        "schedule_id": schedule_id,
                        "patient_name": call.get("patient_name", "Patient"),
                        "doctor": call.get("doctor", "Doctor"),
                        "appointment_time": call.get("appointment_time", ""),
                        "appointment_date": call.get("appointment_date", ""),
                        "reminder_message_tamil": call.get("reminder_message_tamil", ""),
                        "reminder_message_english": call.get("reminder_message_english", ""),
                        "appointment_id": call.get("appointment_id", "")
                    }
                
                update_scheduled_call(schedule_id, {"call_sid": twilio_call.sid})
                print(f"✅ Scheduled call {schedule_id} initiated: {twilio_call.sid}")
                
            except Exception as e:
                print(f"❌ Error executing scheduled call {schedule_id}: {e}")
                
                # Handle retry logic
                retry_count = call.get("retry_count", 0) + 1
                max_retries = call.get("max_retries", 3)
                
                if retry_count >= max_retries:
                    update_scheduled_call(schedule_id, {
                        "status": "failed",
                        "retry_count": retry_count,
                        "error_message": str(e)
                    })
                    print(f"❌ Scheduled call {schedule_id} failed after {retry_count} attempts")
                else:
                    next_retry = datetime.utcnow() + timedelta(minutes=5)
                    update_scheduled_call(schedule_id, {
                        "status": "retrying",
                        "retry_count": retry_count,
                        "next_retry_at": next_retry,
                        "error_message": str(e)
                    })
                    print(f"🔄 Scheduled call {schedule_id} will retry at {next_retry}")
                    
    except Exception as e:
        print(f"❌ Error in execute_due_scheduled_calls: {e}")


async def schedule_appointment_reminders():
    """
    Background job: Check for upcoming appointments and schedule reminder calls.
    Called by APScheduler every hour.
    """
    try:
        # Get appointments in the next 24 hours that don't have reminders scheduled
        upcoming = get_upcoming_appointments(hours_ahead=24)
        
        if not upcoming:
            return
        
        print(f"📅 Found {len(upcoming)} upcoming appointments needing reminders")
        
        for apt in upcoming:
            apt_id = apt.get("id")
            phone = apt.get("user_phone") or apt.get("phone")
            patient_name = apt.get("patient_name", "Patient")
            doctor = apt.get("doctor", "Doctor")
            preferred_dt = apt.get("preferred_datetime", "")
            reason = apt.get("reason", "")
            
            if not phone:
                continue
            
            # Schedule reminder call 1 hour before appointment
            try:
                # Parse appointment time
                if isinstance(preferred_dt, datetime):
                    apt_time = preferred_dt
                else:
                    # Try parsing
                    for fmt in ["%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M"]:
                        try:
                            apt_time = datetime.strptime(str(preferred_dt).strip(), fmt)
                            break
                        except:
                            continue
                    else:
                        continue
                
                # Schedule reminder 1 hour before
                reminder_time = apt_time - timedelta(hours=1)
                
                # Don't schedule if reminder time is in the past
                if reminder_time < datetime.now():
                    reminder_time = datetime.now() + timedelta(minutes=5)
                
                # Format appointment time nicely for reminder
                apt_time_str = apt_time.strftime("%I:%M %p")  # e.g., "03:00 PM"
                apt_date_str = apt_time.strftime("%B %d")  # e.g., "December 26"
                
                # Create bilingual reminder messages
                hosp_name = getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital')
                reminder_tamil = f"வணக்கம் {patient_name}! இது {hosp_name}. உங்க {doctor} appointment நாளை {apt_date_str} அன்று {apt_time_str} க்கு இருக்கு. தயவுசெய்து நேரத்துக்கு வாருங்க. Confirm பண்ண 1 press பண்ணுங்க, cancel பண்ண 2 press பண்ணுங்க."
                reminder_english = f"Hello {patient_name}! This is {hosp_name}. This is a reminder for your appointment with {doctor} on {apt_date_str} at {apt_time_str}. Please arrive 15 minutes early. Press 1 to confirm or 2 to cancel."
                
                schedule_data = {
                    "phone_number": phone,
                    "scheduled_at": reminder_time,
                    "timezone": "Asia/Kolkata",
                    "call_type": "reminder",
                    "appointment_id": apt_id,
                    "patient_name": patient_name,
                    "doctor": doctor,
                    "appointment_time": apt_time_str,
                    "appointment_date": apt_date_str,
                    "reason": reason,
                    "reminder_message_tamil": reminder_tamil,
                    "reminder_message_english": reminder_english,
                    "max_retries": 2
                }
                
                schedule_id = create_scheduled_call(schedule_data)
                
                if schedule_id:
                    mark_appointment_reminder_scheduled(apt_id)
                    print(f"✅ Reminder scheduled for appointment {apt_id}: {reminder_time}")
                    
            except Exception as e:
                print(f"⚠️ Error scheduling reminder for appointment {apt_id}: {e}")
                
    except Exception as e:
        print(f"❌ Error in schedule_appointment_reminders: {e}")


async def cleanup_old_scheduled_calls():
    """
    Background job: Clean up old completed/failed calls.
    Called by APScheduler daily.
    """
    try:
        deleted = delete_old_scheduled_calls(days=30)
        if deleted > 0:
            print(f"🧹 Cleaned up {deleted} old scheduled calls")
    except Exception as e:
        print(f"❌ Error in cleanup_old_scheduled_calls: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize on startup, cleanup on shutdown."""
    global scheduler
    
    print("🚀 Initializing Real-time Voice Agent...")
    print("=" * 60)
    print("🎯 TARGET: Sub-1-second latency")
    print("📡 Mode: Twilio Media Streams + Sarvam WebSocket")
    print("=" * 60)
    init_mongodb()
    
    # Initialize APScheduler for Windows-compatible background jobs
    if MONGODB_AVAILABLE:
        scheduler = AsyncIOScheduler()
        
        # Check for due scheduled calls every minute
        scheduler.add_job(
            execute_due_scheduled_calls,
            IntervalTrigger(minutes=1),
            id="execute_scheduled_calls",
            name="Execute due scheduled calls",
            replace_existing=True
        )
        
        # Check for upcoming appointments every hour and schedule reminders
        scheduler.add_job(
            schedule_appointment_reminders,
            IntervalTrigger(hours=1),
            id="schedule_reminders",
            name="Schedule appointment reminders",
            replace_existing=True
        )
        
        # Cleanup old scheduled calls daily
        scheduler.add_job(
            cleanup_old_scheduled_calls,
            IntervalTrigger(days=1),
            id="cleanup_old_calls",
            name="Cleanup old scheduled calls",
            replace_existing=True
        )
        
        scheduler.start()
        print("⏰ APScheduler started - background jobs active")
        print("   📞 Scheduled calls check: every 1 minute")
        print("   📅 Appointment reminders: every 1 hour")
        print("   🧹 Cleanup old calls: daily")
    else:
        print("⚠️ MongoDB not available - scheduler disabled")
    
    yield
    
    # Shutdown scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        print("⏰ APScheduler stopped")
    
    print("👋 Shutting down...")


# FastAPI app
app = FastAPI(
    title="Real-time Voice Agent",
    description="Sub-1s latency voice agent with Sarvam streaming APIs",
    version="4.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files directory for HTML pages
app.mount("/call_trigger", StaticFiles(directory="call_trigger", html=True), name="call_trigger")

# Active streaming sessions
streaming_sessions: Dict[str, Dict[str, Any]] = {}


# Supported timezones for scheduling
SUPPORTED_TIMEZONES = [
    "Asia/Kolkata",      # IST
    "America/New_York",  # EST
    "America/Los_Angeles", # PST
    "Europe/London",     # GMT/BST
    "UTC"
]


def to_utc(dt_str: str, timezone: str) -> datetime:
    """Convert local datetime string to UTC."""
    if timezone not in SUPPORTED_TIMEZONES:
        raise ValueError(f"Timezone not supported. Use one of: {SUPPORTED_TIMEZONES}")

    local_tz = ZoneInfo(timezone)
    local_dt = datetime.fromisoformat(dt_str)
    local_dt = local_dt.replace(tzinfo=local_tz)

    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # Store as naive UTC


@app.post("/schedule_call")
async def schedule_call_endpoint(payload: dict):
    """
    Schedule a one-time outbound call.
    
    Request body:
    {
        "phone_number": "+919876543210",
        "scheduled_at": "2025-12-23T10:00:00",  # Local time
        "timezone": "Asia/Kolkata",  # IST, EST, PST, GMT, UTC
        "max_retries": 3  # Optional, default 3
    }
    """
    phone_number = payload.get("phone_number")
    scheduled_at = payload.get("scheduled_at")
    timezone = payload.get("timezone", "Asia/Kolkata")
    max_retries = payload.get("max_retries", 3)

    if not all([phone_number, scheduled_at]):
        return JSONResponse(
            status_code=400,
            content={"error": "phone_number and scheduled_at are required"}
        )

    try:
        scheduled_utc = to_utc(scheduled_at, timezone)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    # Check if scheduled time is in the future
    if scheduled_utc < datetime.utcnow():
        return JSONResponse(
            status_code=400,
            content={"error": "Scheduled time must be in the future"}
        )

    # Save to MongoDB
    call_data = {
        "phone_number": phone_number,
        "scheduled_at": scheduled_utc,
        "timezone": timezone,
        "max_retries": max_retries,
        "call_type": "manual"
    }
    
    schedule_id = create_scheduled_call(call_data)
    
    if not schedule_id:
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to create scheduled call. Check MongoDB connection."}
        )

    return {
        "schedule_id": schedule_id,
        "phone_number": phone_number,
        "scheduled_at_utc": scheduled_utc.isoformat(),
        "scheduled_at_local": scheduled_at,
        "timezone": timezone,
        "status": "scheduled"
    }


@app.get("/scheduled_calls")
async def list_scheduled_calls(include_completed: bool = False):
    """
    List all scheduled calls.
    
    Query params:
    - include_completed: bool - Include completed/failed calls (default: false)
    """
    calls = get_all_scheduled_calls(include_completed=include_completed)
    
    # Format datetime objects for JSON
    for call in calls:
        if isinstance(call.get("scheduled_at"), datetime):
            call["scheduled_at"] = call["scheduled_at"].isoformat()
        if isinstance(call.get("next_retry_at"), datetime):
            call["next_retry_at"] = call["next_retry_at"].isoformat()
        if isinstance(call.get("created_at"), datetime):
            call["created_at"] = call["created_at"].isoformat()
        if isinstance(call.get("updated_at"), datetime):
            call["updated_at"] = call["updated_at"].isoformat()
    
    return {
        "count": len(calls),
        "scheduled_calls": calls
    }


@app.get("/scheduled_calls/{schedule_id}")
async def get_scheduled_call_endpoint(schedule_id: str):
    """Get details of a specific scheduled call."""
    call = get_scheduled_call(schedule_id)
    
    if not call:
        return JSONResponse(
            status_code=404,
            content={"error": "Scheduled call not found"}
        )
    
    # Format datetime objects
    if isinstance(call.get("scheduled_at"), datetime):
        call["scheduled_at"] = call["scheduled_at"].isoformat()
    if isinstance(call.get("next_retry_at"), datetime):
        call["next_retry_at"] = call["next_retry_at"].isoformat()
    
    return call


@app.delete("/scheduled_calls/{schedule_id}")
async def cancel_scheduled_call_endpoint(schedule_id: str):
    """Cancel a scheduled call."""
    call = get_scheduled_call(schedule_id)
    
    if not call:
        return JSONResponse(
            status_code=404,
            content={"error": "Scheduled call not found"}
        )
    
    if call.get("status") in ["completed", "failed", "cancelled"]:
        return JSONResponse(
            status_code=400,
            content={"error": f"Cannot cancel call with status: {call.get('status')}"}
        )
    
    success = cancel_scheduled_call(schedule_id)
    
    if success:
        return {"status": "cancelled", "schedule_id": schedule_id}
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to cancel scheduled call"}
        )


@app.post("/scheduled_call_status/{schedule_id}")
async def scheduled_call_status_callback(schedule_id: str, request: Request):
    """
    Twilio status callback for scheduled calls.
    Updates call status based on Twilio's response.
    """
    form = await request.form()
    call_sid = form.get("CallSid")
    status = form.get("CallStatus")
    
    print(f"📞 Scheduled call {schedule_id} status: {status}")
    
    call = get_scheduled_call(schedule_id)
    if not call:
        return {"status": "ok"}
    
    if status in ["no-answer", "busy", "failed"]:
        retry_count = call.get("retry_count", 0) + 1
        max_retries = call.get("max_retries", 3)
        
        if retry_count >= max_retries:
            update_scheduled_call(schedule_id, {
                "status": "failed",
                "retry_count": retry_count,
                "error_message": f"Call {status} after {retry_count} attempts"
            })
            print(f"❌ Scheduled call {schedule_id} failed: {status}")
        else:
            next_retry = datetime.utcnow() + timedelta(minutes=5)
            update_scheduled_call(schedule_id, {
                "status": "retrying",
                "retry_count": retry_count,
                "next_retry_at": next_retry
            })
            print(f"🔄 Scheduled call {schedule_id} will retry at {next_retry}")
            
    elif status == "completed":
        update_scheduled_call(schedule_id, {"status": "completed"})
        print(f"✅ Scheduled call {schedule_id} completed successfully")
    
    return {"status": "ok"}


@app.post("/execute_scheduled_calls")
async def execute_scheduled_calls_endpoint():
    """
    Manually trigger execution of due scheduled calls.
    Note: This runs automatically every minute via APScheduler.
    """
    await execute_due_scheduled_calls()
    return {"status": "ok", "message": "Scheduled calls execution triggered"}


@app.get("/scheduler/status")
async def scheduler_status():
    """Get the status of the background scheduler."""
    global scheduler
    
    if not scheduler:
        return {"status": "disabled", "reason": "MongoDB not available"}
    
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None
        })
    
    return {
        "status": "running" if scheduler.running else "stopped",
        "jobs": jobs
    }

@app.get('/')
async def index():
    """Health and status check."""
    return {
        "status": "active",
        "project": "Anurag AI Hospital — Multilingual AI Voice Assistant",
        "hospital": getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital'),
        "service": "Real-time Voice Agent",
        "mode": "Twilio Media Streams + Sarvam WebSocket",
        "target_latency": "<1 second"
    }


@app.get('/health')
async def health():
    return {"status": "healthy", "mode": "realtime-streaming"}


# ============================================================================
# GOOGLE FILE SEARCH - KNOWLEDGE BASE MANAGEMENT
# ============================================================================

@app.post('/upload_knowledge')
async def upload_knowledge_file(
    file: UploadFile = File(...),
    display_name: Optional[str] = Form(None),
    category: Optional[str] = Form(None)
):
    """
    Upload a document to Google File Search knowledge base.
    
    Supported file types:
    - PDF, DOC, DOCX, PPT, PPTX, XLS, XLSX
    - TXT, MD, HTML, JSON, XML
    
    Args:
        file: The document file to upload
        display_name: Optional custom name (defaults to filename)
        category: Optional category for filtering (e.g., 'pricing', 'schedule')
    
    Returns:
        JSON with upload status and document info
    """
    try:
        from modules.google_file_search import (
            upload_file_to_store,
            get_or_create_file_search_store,
            check_file_search_status
        )
        from pathlib import Path
        import tempfile
        import shutil
        
        # Check if File Search is enabled
        if not getattr(config, 'FILE_SEARCH_ENABLED', False):
            return JSONResponse(
                status_code=400,
                content={"error": "File Search is disabled. Set FILE_SEARCH_ENABLED=true in .env"}
            )
        
        # Check File Search status
        status = check_file_search_status()
        if not status.get('initialized'):
            return JSONResponse(
                status_code=500,
                content={"error": "Google File Search not initialized. Check GOOGLE_API_KEY"}
            )
        
        # Validate file type
        allowed_extensions = [
            '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
            '.txt', '.md', '.html', '.json', '.xml'
        ]
        file_ext = Path(file.filename).suffix.lower()
        
        if file_ext not in allowed_extensions:
            return JSONResponse(
                status_code=400,
                content={
                    "error": f"Unsupported file type: {file_ext}",
                    "allowed": allowed_extensions
                }
            )
        
        # Validate file size (max 100MB as per Google limits)
        max_size = 100 * 1024 * 1024  # 100MB
        file_content = await file.read()
        
        if len(file_content) > max_size:
            return JSONResponse(
                status_code=400,
                content={"error": f"File too large. Max size: 100MB, Got: {len(file_content) / 1024 / 1024:.1f}MB"}
            )
        
        # Save to temporary file
        temp_dir = Path(tempfile.gettempdir()) / "knowledge_uploads"
        temp_dir.mkdir(exist_ok=True)
        
        temp_file_path = temp_dir / file.filename
        with open(temp_file_path, 'wb') as f:
            f.write(file_content)
        
        print(f"📤 Uploading file via API: {file.filename} ({len(file_content) / 1024:.1f} KB)")
        
        # Use display name or filename
        doc_display_name = display_name or file.filename
        
        # Upload to File Search
        result = upload_file_to_store(
            file_path=str(temp_file_path),
            display_name=doc_display_name,
            wait_for_completion=True
        )
        
        # Clean up temp file
        try:
            temp_file_path.unlink()
        except:
            pass
        
        if result:
            return JSONResponse(content={
                "success": True,
                "message": "File uploaded and indexed successfully",
                "document": {
                    "name": result,
                    "display_name": doc_display_name,
                    "original_filename": file.filename,
                    "size_kb": len(file_content) / 1024,
                    "category": category
                }
            })
        else:
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to upload file to File Search"}
            )
            
    except ImportError as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Google File Search module not available: {str(e)}"}
        )
    except Exception as e:
        print(f"❌ Error in upload_knowledge: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get('/knowledge_status')
async def get_knowledge_status():
    """
    Get the status of Google File Search knowledge base.
    
    Returns:
        JSON with File Search status, stores, and configuration
    """
    try:
        from modules.google_file_search import check_file_search_status, list_file_search_stores
        
        status = check_file_search_status()
        
        return JSONResponse(content={
            "enabled": getattr(config, 'FILE_SEARCH_ENABLED', False),
            "initialized": status.get('initialized', False),
            "sdk_available": status.get('sdk_available', False),
            "api_key_set": status.get('api_key_set', False),
            "current_store": status.get('current_store'),
            "stores": status.get('stores', []),
            "model": getattr(config, 'FILE_SEARCH_MODEL', 'gemini-2.5-flash')
        })
        
    except ImportError:
        return JSONResponse(content={
            "enabled": False,
            "error": "Google File Search module not installed"
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post('/search_knowledge')
async def search_knowledge(request: Request):
    """
    Search the knowledge base with a query.
    
    Request body:
        {
            "query": "What are the hospital timings?",
            "metadata_filter": "category=pricing"  // optional
        }
    
    Returns:
        JSON with search results and answer
    """
    try:
        from modules.google_file_search import search_knowledge_base
        
        # Check if File Search is enabled
        if not getattr(config, 'FILE_SEARCH_ENABLED', False):
            return JSONResponse(
                status_code=400,
                content={"error": "File Search is disabled"}
            )
        
        data = await request.json()
        query = data.get('query', '')
        metadata_filter = data.get('metadata_filter')
        
        if not query:
            return JSONResponse(
                status_code=400,
                content={"error": "Query is required"}
            )
        
        print(f"🔍 Knowledge search via API: {query[:50]}...")
        
        result = search_knowledge_base(
            query=query,
            metadata_filter=metadata_filter
        )
        
        if result.get('error'):
            return JSONResponse(
                status_code=500,
                content={"error": result['error']}
            )
        
        return JSONResponse(content={
            "success": True,
            "query": query,
            "answer": result.get('answer'),
            "citations": result.get('citations', []),
            "search_time_seconds": result.get('search_time', 0)
        })
        
    except Exception as e:
        print(f"❌ Error in search_knowledge: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.delete('/delete_knowledge_store')
async def delete_knowledge_store(store_name: str = None):
    """
    Delete a File Search store (admin endpoint).
    
    Args:
        store_name: The store name to delete (optional, uses default if not provided)
    
    Returns:
        JSON with deletion status
    """
    try:
        from modules.google_file_search import (
            delete_file_search_store,
            list_file_search_stores,
            _file_search_store_name
        )
        
        # Get store name to delete
        if not store_name:
            stores = list_file_search_stores()
            if stores:
                store_name = stores[0]['name']
            else:
                return JSONResponse(
                    status_code=404,
                    content={"error": "No File Search stores found"}
                )
        
        success = delete_file_search_store(store_name, force=True)
        
        if success:
            return JSONResponse(content={
                "success": True,
                "message": f"Deleted store: {store_name}"
            })
        else:
            return JSONResponse(
                status_code=500,
                content={"error": f"Failed to delete store: {store_name}"}
            )
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post('/voice')
@app.post('/outbound_voice')
async def handle_voice(request: Request):
    """
    Handle incoming/outbound call - Start Media Stream.
    Returns TwiML to connect caller to our WebSocket.
    """
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid', '')
        from_number = form_data.get('From', '')  # Twilio number for outbound
        to_number = form_data.get('To', '')  # Patient number for outbound
        direction = form_data.get('Direction', 'inbound')  # 'outbound-api' or 'inbound'
        
        # For outbound calls, the patient is 'To', for inbound, patient is 'From'
        is_outbound = 'outbound' in direction.lower()
        patient_phone = to_number if is_outbound else from_number
        
        print("🔥 /make_call HIT 🔥")
        print(f"\n{'='*60}")
        print(f"📞 NEW CALL: {from_number}")
        print(f"📞 CallSid: {call_sid}")
        print(f"📞 Direction: {direction}")
        print(f"📞 Patient Phone: {patient_phone}")
        print(f"{'='*60}\n")
        
        response = VoiceResponse()
        
        # Connect directly to Media Stream WebSocket
        # Our TTS will handle the greeting
        connect = Connect()
        stream = Stream(
            url=f"wss://{config.NGROK_URL}/media-stream",
            track="inbound_track"  # Receive caller's audio
        )
        # Add custom parameters
        stream.parameter(name="CallSid", value=call_sid)
        stream.parameter(name="Caller", value=patient_phone)  # Use patient phone, not Twilio number
        stream.parameter(name="Direction", value=direction)
        
        connect.append(stream)
        response.append(connect)
        
        return Response(content=str(response), media_type="application/xml")
    except Exception as e:
        print(f"❌ ERROR in handle_voice: {e}")
        import traceback
        traceback.print_exc()
        
        # Return error TwiML
        error_response = VoiceResponse()
        error_response.say("Sorry, there was an error. Please try again later.")
        error_response.hangup()
        return Response(content=str(error_response), media_type="application/xml")


@app.post('/reminder_voice/{schedule_id}')
async def handle_reminder_voice(request: Request, schedule_id: str):
    """
    Handle reminder outbound call - Start Media Stream with reminder context.
    Similar to outbound_voice but passes reminder info to the WebSocket.
    """
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid', '')
        from_number = form_data.get('From', '')
        to_number = form_data.get('To', '')
        direction = form_data.get('Direction', 'outbound-api')
        
        patient_phone = to_number
        
        print(f"\n{'='*60}")
        print(f"📞 REMINDER CALL: {patient_phone}")
        print(f"📞 CallSid: {call_sid}")
        print(f"📞 Schedule ID: {schedule_id}")
        print(f"{'='*60}\n")
        
        response = VoiceResponse()
        
        # Connect to Media Stream WebSocket with reminder parameters
        connect = Connect()
        stream = Stream(
            url=f"wss://{config.NGROK_URL}/media-stream",
            track="inbound_track"
        )
        # Add custom parameters including reminder flag
        stream.parameter(name="CallSid", value=call_sid)
        stream.parameter(name="Caller", value=patient_phone)
        stream.parameter(name="Direction", value=direction)
        stream.parameter(name="CallType", value="reminder")
        stream.parameter(name="ScheduleId", value=schedule_id)
        
        connect.append(stream)
        response.append(connect)
        
        return Response(content=str(response), media_type="application/xml")
    except Exception as e:
        print(f"❌ ERROR in handle_reminder_voice: {e}")
        import traceback
        traceback.print_exc()
        
        error_response = VoiceResponse()
        error_response.say("Sorry, there was an error with the reminder call.")
        error_response.hangup()
        return Response(content=str(error_response), media_type="application/xml")


@app.websocket('/media-stream')
async def media_stream_websocket(websocket: WebSocket):
    """
    Handle Twilio Media Stream WebSocket connection.
    This is where real-time audio streaming happens.
    """
    await websocket.accept()
    
    session_id = str(uuid.uuid4())[:8]
    call_sid = ""
    stream_sid = ""
    caller = ""
    
    # Session state
    session = {
        "id": session_id,
        "audio_buffer": AudioBuffer(target_duration_ms=40),  # ⚡ Smaller chunks for faster VAD
        "silence_detector": SilenceDetector(threshold=250, silence_duration_ms=800, min_speech_duration_ms=800),  # ⚡ 0.8s silence after 0.8s speech - faster turns
        "accumulated_audio": b"",
        "language": "ta-IN",  # Default to Tamil for Chennai hospital
        "locked_language": "ta-IN",  # Start locked to Tamil
        "language_streak": 0,  # Count consecutive turns in same detected language
        "last_detected_lang": None,  # Track last STT detected language
        "garbled_count": 0,  # Count consecutive turns with garbled/misunderstood transcripts
        "conversation": [],
        "is_processing": False,
        "start_time": time.perf_counter(),
        "turn_count": 0,
        "booking_confirmed": False,  # Track if booking was confirmed
        "asked_other_questions": False,  # Track if agent asked "any other questions?"
        "farewell_given": False,  # Track if agent already said goodbye
        "collected_info": {  # Track what info we've collected to avoid re-asking
            "name": None,
            "doctor": None,
            "date": None,
            "time": None,
            "reason": None
        },
        "call_should_end": False,  # Flag to end call gracefully
        "call_ended": False,  # Flag to stop processing after call ends
        "is_speaking": False,  # ⚡ Flag to prevent barge-in while TTS is playing
        "playback_end_time": 0,  # ⚡ Track when playback ends for early listening
        # ⚡ Persistent connections for lower latency
        "stt_client": None,  # Will be initialized on first use
        "tts_client": None,  # Will be initialized on first use
        "processing_lock": asyncio.Lock(),  # ⚡ Prevents concurrent STT/TTS access
        # ⚡ TRUE STREAMING: New streaming STT client that receives audio chunks in real-time
        "streaming_stt": None,  # TrueStreamingSTT instance - kept open for entire call
        "streaming_stt_active": False,  # Whether streaming STT is actively receiving audio
        "vad_speech_ended": False,  # Flag set when Sarvam VAD detects END_SPEECH
    }
    
    # Timing metrics
    timing = {
        "turn_start": None,
        "stt_start": None,
        "llm_start": None,
        "tts_start": None
    }
    
    print(f"🔌 WebSocket connected: {session_id}")
    
    try:
        async for message in websocket.iter_text():
            # Check if call should end
            if session.get("call_ended", False):
                print("📞 Call ended flag set, stopping message processing")
                break
                
            data = json.loads(message)
            event_type = data.get('event')
            
            if event_type == 'connected':
                print(f"✅ Media Stream connected")
                # ⚡ PRE-CREATE TTS CONNECTION: Start creating TTS connection early
                # Store the task so we can await it before greeting
                async def prewarm_tts():
                    try:
                        tts = SarvamRealtimeTTS(language="ta-IN", speaker="anushka")  # Default Tamil
                        if await tts.connect(max_retries=2, retry_delay=0.1):
                            session["tts_client"] = tts
                            print("✅ TTS connection pre-warmed")
                            return True
                    except Exception as e:
                        print(f"⚠️ TTS prewarm failed: {e}")
                    return False
                session["tts_prewarm_task"] = asyncio.create_task(prewarm_tts())
                
            elif event_type == 'start':
                # Stream started - extract metadata
                start_data = data.get('start', {})
                stream_sid = start_data.get('streamSid', '')
                call_sid = start_data.get('callSid', '')
                
                # Get custom parameters
                custom_params = start_data.get('customParameters', {})
                caller = custom_params.get('Caller', '')
                call_type = custom_params.get('CallType', 'regular')  # 'regular' or 'reminder'
                schedule_id = custom_params.get('ScheduleId', '')
                
                session["call_sid"] = call_sid
                session["stream_sid"] = stream_sid
                session["caller"] = caller
                session["call_type"] = call_type
                session["schedule_id"] = schedule_id
                
                # If reminder call, get reminder data
                if call_type == "reminder" and call_sid in reminder_call_data:
                    session["reminder_data"] = reminder_call_data[call_sid]
                    print(f"📋 Reminder call data loaded for {call_sid}")
                
                streaming_sessions[call_sid] = session
                
                print(f"🎙️ Stream started: {stream_sid}")
                print(f"📞 Call: {call_sid}, Caller: {caller}, Type: {call_type}")
                
                # ⚡ FAST GREETING: Wait for TTS prewarm (max 2s) then send greeting
                async def send_fast_greeting():
                    # Wait for TTS prewarm to complete (should be ~1-2s)
                    prewarm_task = session.get("tts_prewarm_task")
                    if prewarm_task:
                        try:
                            await asyncio.wait_for(prewarm_task, timeout=2.5)
                        except asyncio.TimeoutError:
                            print("⚠️ TTS prewarm timeout, will create new connection")
                    await send_greeting(websocket, stream_sid, session)
                
                greeting_task = asyncio.create_task(send_fast_greeting())
                
                # ⚡ BACKGROUND: Initialize STT and pre-warm LLM in parallel
                async def initialize_stt_background():
                    """Initialize STT in background while greeting plays."""
                    try:
                        streaming_stt = TrueStreamingSTT(
                            language=session.get("locked_language") or "unknown",
                            sample_rate=16000
                        )
                        if await streaming_stt.connect():
                            session["streaming_stt"] = streaming_stt
                            
                            async def on_vad_event(event_type: str, turn_id: int):
                                stt = session.get("streaming_stt")
                                if stt and turn_id == stt.get_current_turn_id():
                                    if event_type == "END_SPEECH":
                                        session["vad_speech_ended"] = True
                            
                            await streaming_stt.start_listening(on_vad_event=on_vad_event)
                            session["streaming_stt_active"] = True
                            print("✅ Streaming STT initialized (background)")
                        else:
                            print("⚠️ Failed to initialize streaming STT")
                    except Exception as e:
                        print(f"⚠️ STT initialization error: {e}")
                
                # Start STT and LLM prewarm in parallel (non-blocking)
                asyncio.create_task(initialize_stt_background())
                asyncio.create_task(prewarm_llm_connection())
                
                # 🎵 START BACKGROUND AMBIENT AUDIO LOOP
                # Plays very mild ambient sound throughout call to keep it feeling alive
                if BACKGROUND_AUDIO_LOADED:
                    bg_audio_task = asyncio.create_task(
                        start_background_audio_loop(websocket, stream_sid, session)
                    )
                    session["bg_audio_task"] = bg_audio_task
                    print("🎵 Background ambient audio started")
                
            elif event_type == 'media':
                # Received audio chunk from caller
                media_data = data.get('media', {})
                payload = media_data.get('payload', '')
                
                if payload:
                    # Decode base64 mu-law audio
                    mulaw_audio = base64.b64decode(payload)
                    
                    # ECHO PREVENTION: Skip audio during TTS playback
                    # BUT: Allow audio accumulation near end of playback for faster response
                    playback_end = session.get("playback_end_time", 0)
                    if playback_end > 0:
                        time_remaining = playback_end - time.time()
                        if time_remaining > 0.1:  # Reduced from 0.3s to 0.1s - start listening earlier
                            continue  # Still playing, skip
                        else:
                            # Near end of playback - reset flags to allow capture
                            session["playback_end_time"] = 0
                            session["is_speaking"] = False
                    
                    # Skip if still marked as speaking (backup check)
                    if session.get("is_speaking", False):
                        # Safety timeout - if is_speaking stuck for > 10s, reset it
                        if time.time() - session.get("_speaking_start", time.time()) > 10:
                            print("⚠️ is_speaking stuck - resetting")
                            session["is_speaking"] = False
                        else:
                            continue
                    
                    # Skip audio while processing (STT/LLM phase)
                    if session["is_processing"]:
                        continue
                    
                    # Add to buffer and get PCM when ready
                    pcm_chunk = session["audio_buffer"].add_chunk(mulaw_audio)
                    
                    if pcm_chunk:
                        # ⚡ TRUE STREAMING: Send audio chunk to STT immediately
                        streaming_stt = session.get("streaming_stt")
                        if streaming_stt and session.get("streaming_stt_active"):
                            await streaming_stt.stream_chunk(pcm_chunk)
                        
                        # Also accumulate for backup/silence detection
                        session["accumulated_audio"] += pcm_chunk
                        
                        # Calculate current audio duration
                        audio_duration_samples = len(session["accumulated_audio"]) // 2  # 16-bit = 2 bytes per sample
                        audio_duration_sec = audio_duration_samples / 16000
                        
                        # ⚡ FORCE PROCESS if audio exceeds 8 seconds (avoid long waits)
                        force_process = audio_duration_sec >= 8.0
                        
                        # ⚡ TRUE STREAMING: Check Sarvam VAD END_SPEECH signal OR local silence detector
                        sarvam_vad_ended = session.get("vad_speech_ended", False)
                        local_vad_ended = session["silence_detector"].process(pcm_chunk)
                        
                        # Double-check: If Sarvam VAD triggered, validate it's from current turn
                        streaming_stt = session.get("streaming_stt")
                        if sarvam_vad_ended and streaming_stt:
                            if not streaming_stt.is_vad_event_valid():
                                # Stale VAD event from previous turn - ignore it
                                session["vad_speech_ended"] = False
                                sarvam_vad_ended = False
                        
                        # Check for end of speech - REQUIRE VAD detection (not just silence)
                        # sarvam_vad_ended or local_vad_ended means actual speech was detected
                        speech_detected = sarvam_vad_ended or local_vad_ended
                        
                        if (speech_detected or force_process) and not session["is_processing"]:
                            # Require minimum 0.8 seconds of audio (12800 samples at 16kHz)
                            # This filters out noise/echo that's < 0.8s
                            min_samples = 12800  # 0.8 seconds
                            if len(session["accumulated_audio"]) > min_samples:
                                session["is_processing"] = True
                                session["is_speaking"] = True  # Block audio accumulation during processing
                                session["turn_count"] += 1
                                print(f"\n🎤 Turn {session['turn_count']} ({audio_duration_sec:.1f}s)")
                                timing["turn_start"] = time.perf_counter()
                                session["vad_speech_ended"] = False
                                
                                if session.get("call_ended", False):
                                    session["is_processing"] = False
                                    session["is_speaking"] = False
                                    continue
                                
                                try:
                                    await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                                except:
                                    pass
                                
                                asyncio.create_task(
                                    process_speech_streaming(websocket, stream_sid, session, timing)
                                )
                            elif len(session["accumulated_audio"]) > 4000:  # > 0.25s but < 0.8s
                                # Too short - likely noise, reset
                                session["accumulated_audio"] = b""
                                session["silence_detector"].reset()
                                session["vad_speech_ended"] = False
                
            elif event_type == 'stop':
                print(f"🛑 Stream stopped - Call ended by Twilio")
                print(f"   Session duration: {time.perf_counter() - session['start_time']:.1f}s")
                print(f"   Total turns: {session['turn_count']}")
                session["call_ended"] = True  # Signal all tasks to stop
                
                # 🎵 Cancel background audio task
                bg_task = session.get("bg_audio_task")
                if bg_task and not bg_task.done():
                    bg_task.cancel()
                    print("🎵 Background audio cancelled")
                
                # ⚡ Clean up streaming STT
                if session.get("streaming_stt"):
                    await session["streaming_stt"].disconnect()
                break
                
            elif event_type == 'mark':
                # Playback done - enable listening with fresh buffers
                session["playback_end_time"] = 0
                session["is_speaking"] = False
                # CRITICAL: Clear any residual audio to prevent false triggers
                session["accumulated_audio"] = b""
                session["silence_detector"].reset()
                session["audio_buffer"].clear()
                session["vad_speech_ended"] = False
    
    except WebSocketDisconnect:
        print(f"🔌 WebSocket disconnected: {session_id}")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 📊 Save call data to MongoDB and Google Sheets
        try:
            conversation_history = session.get("conversation", [])
            caller_phone = session.get("caller", "")
            
            print(f"📊 Call ended - Caller: {caller_phone}, Turns: {session.get('turn_count', 0)}")
            
            if conversation_history and len(conversation_history) > 1:
                print("📊 Extracting appointment data from conversation...")
                
                # Use LLM to extract appointment details
                appointment_data = extract_appointment_with_llm(conversation_history)
                
                print(f"📊 Extracted data: {appointment_data}")
                
                if appointment_data:
                    # Map extracted data to expected format
                    formatted_data = {
                        "name": appointment_data.get("patient_name"),
                        "doctor": appointment_data.get("doctor_type"),
                        "date_time": f"{appointment_data.get('date', '')} {appointment_data.get('time', '')}".strip(),
                        "reason": appointment_data.get("reason"),
                        "additional_info": appointment_data.get("additional_info"),
                        "phone": caller_phone,  # Explicitly add phone
                        "call_sid": call_sid  # Explicitly add call_sid
                    }
                    
                    print(f"📊 Formatted data for saving: {formatted_data}")
                    
                    # Save to MongoDB and Google Sheets
                    save_result = save_appointment_to_mongodb_and_sheets(
                        user_phone=caller_phone,
                        appointment_details=formatted_data,
                        session_id=call_sid,
                        conversation_history=conversation_history
                    )
                    
                    if save_result:
                        print(f"✅ Call data saved successfully!")
                    else:
                        print(f"⚠️ Could not save call data (incomplete booking)")
                else:
                    print("⚠️ No appointment data extracted from conversation")
            else:
                print("⚠️ No conversation history to save")
                
        except Exception as save_error:
            print(f"❌ Error saving call data: {save_error}")
            import traceback
            traceback.print_exc()
        
        # ⚡ Cleanup persistent STT/TTS connections
        if session.get("streaming_stt"):
            try:
                await session["streaming_stt"].disconnect()
                print("🧹 Streaming STT cleaned up")
            except:
                pass
        if session.get("stt_client"):
            try:
                await session["stt_client"].disconnect()
            except:
                pass
        if session.get("tts_client"):
            try:
                await session["tts_client"].disconnect()
            except:
                pass
        # Cleanup reminder call data if this was a reminder call
        if call_sid in reminder_call_data:
            del reminder_call_data[call_sid]
            print(f"🧹 Reminder call data cleaned up for {call_sid}")
        # Cleanup session
        if call_sid in streaming_sessions:
            del streaming_sessions[call_sid]
        print(f"🧹 Session cleaned up: {session_id}")


# ============================================================================
# STREAMING LLM CLIENT - For ultra-low latency
# ============================================================================

class StreamingLLMClient:
    """
    Async streaming LLM client for Sarvam API.
    Yields tokens as they arrive for sentence-level TTS.
    """
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session with connection reuse"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=30)
            timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=15)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session
    
    async def stream(self, messages: list, max_tokens: int = 150, temperature: float = 0.3):
        """
        Stream LLM response tokens.
        
        Yields: str tokens as they arrive
        """
        session = await self._get_session()
        
        endpoint = f"{config.LLM_API_URL}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "api-subscription-key": config.SARVAM_API_KEY
        }
        payload = {
            "model": config.LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True
        }
        
        try:
            async with session.post(endpoint, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    print(f"❌ LLM stream error: {resp.status} - {error[:100]}")
                    return
                
                buffer = ""
                async for chunk in resp.content.iter_any():
                    buffer += chunk.decode('utf-8')
                    
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        
                        if not line.startswith('data: '):
                            continue
                        
                        data = line[6:]
                        if data == '[DONE]':
                            return
                        
                        try:
                            parsed = json.loads(data)
                            delta = parsed.get('choices', [{}])[0].get('delta', {})
                            content = delta.get('content')
                            if content:
                                yield content
                        except:
                            continue
        except Exception as e:
            print(f"❌ LLM stream error: {e}")
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Global streaming LLM client (connection reuse)
_streaming_llm: Optional[StreamingLLMClient] = None

def get_streaming_llm() -> StreamingLLMClient:
    global _streaming_llm
    if _streaming_llm is None:
        _streaming_llm = StreamingLLMClient()
    return _streaming_llm


async def get_or_create_tts(session: dict, language: str, speaker: str = "anushka") -> Optional[SarvamRealtimeTTS]:
    """
    ⚡ PERSISTENT TTS: Get existing TTS connection or create new one.
    Reuses connection across turns to save ~1-1.5s per turn.
    """
    tts = session.get("tts_client")
    
    # Check if existing connection is still valid
    if tts and tts.is_connected():
        # Reconfigure if language changed
        if tts.language != language or tts.speaker != speaker:
            print(f"🔄 Reconfiguring TTS: {tts.language} → {language}")
            await tts.reconfigure(language, speaker)
        else:
            # Same language - just reset for new stream
            await tts.reset_for_stream()
        return tts
    
    # Need to create new connection
    if tts:
        try:
            await tts.disconnect()
        except:
            pass
    
    print(f"🔊 Creating new TTS connection for {language}")
    new_tts = SarvamRealtimeTTS(language=language, speaker=speaker)
    if await new_tts.connect(max_retries=2, retry_delay=0.1):
        session["tts_client"] = new_tts
        return new_tts
    
    print("❌ Failed to create TTS connection")
    return None


async def prewarm_llm_connection():
    """⚡ Pre-warm LLM connection to eliminate cold-start delay."""
    try:
        llm = get_streaming_llm()
        await llm._get_session()
        print("✅ LLM connection pre-warmed")
    except Exception as e:
        print(f"⚠️ LLM prewarm failed: {e}")


async def send_greeting(websocket: WebSocket, stream_sid: str, session: dict):
    """Send initial greeting using Sarvam TTS - Tamil first, or reminder message for reminder calls."""
    try:
        call_type = session.get("call_type", "regular")
        
        if call_type == "reminder":
            # This is a reminder call - use custom reminder greeting
            reminder_data = session.get("reminder_data", {})
            patient_name = reminder_data.get("patient_name", "")
            doctor = reminder_data.get("doctor", "Doctor")
            apt_time = reminder_data.get("appointment_time", "")
            apt_date = reminder_data.get("appointment_date", "today")
            
            hosp_name = getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital')
            # Professional reminder greeting in Tamil
            if patient_name:
                greeting = f"வணக்கம் {patient_name}! இது {hosp_name} இருந்து அழைக்கிறோம். உங்களோட {doctor} appointment {apt_date} அன்று {apt_time} க்கு இருக்கு. தயவுசெய்து 15 நிமிடம் முன்னாடியே வாருங்க. உங்க appointment confirm ஆ?"
            else:
                greeting = f"வணக்கம்! இது {hosp_name} இருந்து அழைக்கிறோம். உங்களோட {doctor} appointment {apt_date} அன்று {apt_time} க்கு இருக்கு. தயவுசெய்து நேரத்துக்கு வாருங்க. உங்க appointment confirm ஆ?"
            
            print(f"📋 Sending reminder greeting for {patient_name}")
            
            # Add reminder context to conversation for LLM
            session["conversation_history"] = session.get("conversation_history", [])
            session["conversation_history"].append({
                "role": "system",
                "content": f"This is a REMINDER CALL from {hosp_name}. Patient: {patient_name}, Doctor: {doctor}, Appointment: {apt_date} at {apt_time}. Your job is to confirm the appointment. If patient confirms, say 'நன்றி, உங்க appointment confirm ஆகிவிட்டது'. If patient wants to cancel or reschedule, note it and say you'll update the hospital."
            })
            
            await send_tts_response(websocket, stream_sid, greeting, session, "ta-IN")
        else:
            # Regular call - Tamil-first bilingual greeting (Chennai hospital)
            greeting = f"வணக்கம்! {getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital')}-க்கு உங்களை வரவேற்கிறேன். தமிழ் அல்லது English லும் பேசலாம். என்ன உதவி வேணும்?"
            
            await send_tts_response(websocket, stream_sid, greeting, session, "ta-IN")
    except Exception as e:
        print(f"❌ Error in send_greeting: {e}")
        import traceback
        traceback.print_exc()


async def process_speech_streaming(
    websocket: WebSocket,
    stream_sid: str,
    session: dict,
    timing: dict
):
    """
    ⚡ TRUE STREAMING: Process user speech using streaming STT
    
    This version uses the pre-streamed audio from TrueStreamingSTT.
    Audio was already being sent to STT in real-time as it arrived from Twilio.
    We just need to flush and get the final transcript.
    
    Pipeline: [Audio already streamed] → Flush → Get Transcript → LLM → TTS
    
    Expected latency savings:
    - OLD: Wait for all audio → Send to STT → Wait for transcript (~1.5s)
    - NEW: Audio already sent → Flush → Get transcript (~0.3s)
    """
    # 🛑 Check if call has ended before processing
    if session.get("call_ended", False):
        print("🛑 Call ended - skipping speech processing")
        return
    
    # ⚡ Use lock to prevent concurrent access
    lock = session.get("processing_lock")
    if lock and lock.locked():
        print("⏭️ Skipping - another turn is still processing")
        return
    
    async with lock if lock else asyncio.Lock():
        # is_processing already set before task was created
        audio_duration = len(session["accumulated_audio"]) / 2 / 16000
        
        # Store audio for fallback before clearing
        session["_fallback_audio"] = session["accumulated_audio"]
        
        # Clear buffers for next utterance
        session["accumulated_audio"] = b""
        session["silence_detector"].reset()
        session["audio_buffer"].clear()
        
        # 🛑 Check again in case call ended during buffer clearing
        if session.get("call_ended", False):
            print("🛑 Call ended during buffer clear - aborting")
            session["is_processing"] = False
            return
        
        try:
            # ⏱️ STT - Use streaming STT that already has the audio
            timing["stt_start"] = time.perf_counter()
            
            streaming_stt = session.get("streaming_stt")
            transcript = ""
            detected_language = session.get("locked_language") or "en-IN"
            
            if streaming_stt and session.get("streaming_stt_active"):
                # ⚡ Ensure streaming STT is healthy
                if not streaming_stt.is_healthy():
                    print("⚠️ Streaming STT unhealthy, attempting recovery...")
                    await streaming_stt.ensure_healthy()
                
                # ⚡ TRUE STREAMING: Flush and get transcript
                await streaming_stt.flush()
                
                try:
                    result = await asyncio.wait_for(
                        streaming_stt.get_final_transcript(timeout=4.0),
                        timeout=5.0
                    )
                    if result:
                        transcript = result.transcript
                        if result.language_code and result.language_code != "unknown":
                            detected_language = result.language_code
                        
                except asyncio.TimeoutError:
                    transcript = ""
                
                # Reset for next turn (keep connection open)
                await streaming_stt.reset_for_new_turn()
            
            # Fallback to batch STT if streaming failed
            if not transcript.strip():
                audio_data = session.get("_fallback_audio", b"")
                if audio_data:
                    stt = SarvamRealtimeSTT(language=session.get("locked_language") or "unknown")
                    if await stt.connect(max_retries=2, retry_delay=0.1):
                        trimmed_audio = trim_silence(audio_data, threshold=300, padding_ms=300)
                        wav_audio = pcm_to_wav(trimmed_audio, sample_rate=16000, sample_width=2)
                        await stt.send_audio(wav_audio, encoding="audio/wav")
                        await stt.flush()
                        try:
                            result = await asyncio.wait_for(stt.receive(), timeout=6.0)
                            if result:
                                transcript = result.transcript
                                detected_language = result.language_code or detected_language
                        except asyncio.TimeoutError:
                            pass
                        await stt.disconnect()
            
            # --- SMART LANGUAGE DETECTION ---
            # 1. Check if user explicitly requests language switch
            requested_lang = detect_language_switch_request(transcript)
            if requested_lang:
                session["locked_language"] = requested_lang
                session["language"] = requested_lang
                session["language_streak"] = 0
                print(f"🔒 Language: {requested_lang}")
                
                # Update streaming STT language for next turn
                if streaming_stt:
                    streaming_stt.language = requested_lang
            elif session["locked_language"]:
                session["language"] = session["locked_language"]
            else:
                # Auto-detect from script
                script_lang, script_chars = detect_script_language(transcript)
                if script_lang and script_chars >= 3:
                    lang_mapping = {
                        'ta-IN': 'ta-IN', 'hi-IN': 'hi-IN', 'te-IN': 'te-IN',
                        'kn-IN': 'ta-IN', 'ml-IN': 'ta-IN', 'bn-IN': 'hi-IN',
                        'pa-IN': 'hi-IN', 'mr-IN': 'hi-IN', 'gu-IN': 'hi-IN', 'or-IN': 'hi-IN',
                    }
                    mapped_lang = lang_mapping.get(script_lang, 'en-IN')
                    if mapped_lang != session["language"]:
                        session["language"] = mapped_lang
            
            # Check if call ended during STT
            if session.get("call_ended", False):
                session["is_processing"] = False
                return
            
            stt_time = time.perf_counter() - timing["stt_start"]
            print(f"👤 User: '{transcript}'")
            
            # Handle empty transcript
            if not transcript.strip() or len(transcript.strip()) < 2:
                if session.get("call_ended", False):
                    session["is_processing"] = False
                    return
                
                session["garbled_count"] = session.get("garbled_count", 0) + 1
                if session["garbled_count"] >= 3:
                    session["garbled_count"] = 0
                    lang_prompt = "I'm having trouble understanding. Would you like to speak in Tamil, Hindi, or continue in English?"
                    await send_tts_response(websocket, stream_sid, lang_prompt, session, "en-IN")
                else:
                    repeat_messages = {
                        'en-IN': "Sorry, I couldn't hear that clearly. Could you please repeat?",
                        'ta-IN': "மன்னிக்கவும், சரியா கேக்கல. மீண்டும் சொல்ல முடியுமா?",
                        'hi-IN': "माफ़ कीजिए, मुझे सुनाई नहीं दिया। कृपया फिर से बोलिए?",
                        'te-IN': "క్షమించండి, నాకు వినిపించలేదు. దయచేసి మళ్ళీ చెప్పగలరా?"
                    }
                    repeat_msg = repeat_messages.get(session["language"], repeat_messages['en-IN'])
                    await send_tts_response(websocket, stream_sid, repeat_msg, session, session["language"])
                session["is_processing"] = False
                return
            
            # Reset garbled counter on good transcript
            session["garbled_count"] = 0
            
            # Check for goodbye
            if detect_goodbye(transcript, session["language"]):
                has_min_info = len(session["conversation"]) >= 6
                explicit_bye_words = ['bye', 'goodbye', 'பை', 'बाय', 'బై', 'alvida', 'போறேன்', 'चलता', 'వెళ్తా']
                is_explicit_bye = any(word in transcript.lower() for word in explicit_bye_words)
                
                if is_explicit_bye or has_min_info:
                    print(f"👋 User said goodbye: '{transcript}' - ending call gracefully")
                    goodbye_lang = session.get("locked_language") or session["language"]
                    hosp_name = getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital')
                    goodbye_messages = {
                        'en-IN': f'Thank you for calling {hosp_name}. Take care and have a great day!',
                        'ta-IN': f'{hosp_name}-க்கு கால் பண்ணதுக்கு ரொம்ப நன்றி. உங்களை சந்திப்போம்!',
                        'hi-IN': f'{hosp_name} को कॉल करने के लिए धन्यवाद। अपना ख्याल रखिए!',
                        'te-IN': f'{hosp_name}కు కాల్ చేసినందుకు చాలా ధన్యవాదాలు. మిమ్మల్ని కలుస్తాం!'
                    }
                    goodbye_text = goodbye_messages.get(goodbye_lang, goodbye_messages['en-IN'])
                    await send_tts_response(websocket, stream_sid, goodbye_text, session, goodbye_lang)
                    await asyncio.sleep(3)
                    session["call_ended"] = True
                    session["is_processing"] = False
                    try:
                        await websocket.close()
                        print("✔️ WebSocket closed - call terminated")
                    except Exception as e:
                        print(f"ℹ️ WebSocket already closed: {e}")
                    return
            
            # 🛑 Check if call ended before LLM
            if session.get("call_ended", False):
                print("🛑 Call ended before LLM - aborting")
                session["is_processing"] = False
                return
            
            # ⏱️ LLM + TTS (STREAMING PIPELINE) - Same as original
            timing["llm_start"] = time.perf_counter()
            
            lang_names = {'en-IN': 'English', 'hi-IN': 'Hindi', 'ta-IN': 'Tamil', 'te-IN': 'Telugu'}
            lang_name = lang_names.get(session["language"], "English")
            
            current_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %B %d, %Y")
            
            # Add native script enforcement prefix for non-English
            script_prefix = ""
            if session["language"] == "ta-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Tamil script (தமிழ்). NEVER use Roman/English letters for Tamil words. Example: Write 'சரி, உங்க பேரு என்ன?' NOT 'Sari, unga peru enna?'\n\n"
            elif session["language"] == "hi-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Hindi script (हिंदी). NEVER use Roman/English letters for Hindi words. Example: Write 'ठीक है, आपका नाम क्या है?' NOT 'Theek hai, aapka naam kya hai?'\n\n"
            elif session["language"] == "te-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Telugu script (తెలుగు). NEVER use Roman/English letters for Telugu words.\n\n"
            
            system_prompt = script_prefix + config.HOSPITAL_SYSTEM_PROMPT.format(
                user_language=lang_name,
                current_date=current_date
            )
            
            # ============================================================
            # 🔍 SMART RAG - Check if query needs knowledge base
            # ============================================================
            rag_context = None
            use_rag = should_use_rag(transcript, session["language"])
            
            if use_rag and getattr(config, 'FILE_SEARCH_ENABLED', False):
                try:
                    from modules.google_file_search import get_relevant_context
                    print(f"🔍 RAG triggered for: '{transcript[:50]}...'")
                    
                    rag_start = time.perf_counter()
                    # Pass session language so Gemini responds in Tamil/Hindi/Telugu directly
                    rag_context = get_relevant_context(
                        query=transcript,
                        max_chars=1000,  # Keep context small for speed
                        response_language=session["language"]  # e.g., 'ta-IN' for Tamil
                    )
                    rag_time = time.perf_counter() - rag_start
                    
                    if rag_context:
                        print(f"✅ RAG context retrieved ({len(rag_context)} chars) in {rag_time:.2f}s")
                        print(f"   📝 RAG Response: {rag_context[:100]}...")
                    else:
                        print(f"⚠️ No relevant RAG context found ({rag_time:.2f}s)")
                except Exception as e:
                    print(f"⚠️ RAG error (continuing without context): {e}")
            
            # Add RAG context to system prompt if available
            if rag_context:
                system_prompt += f"""

📚 KNOWLEDGE BASE INFORMATION (use this to answer the user's question):
---
{rag_context}
---
Use the above information to provide accurate answers about pricing, timings, doctors, etc.
"""
            
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(session["conversation"][-20:])
            
            # Add explicit language reminder for non-English languages
            if session["language"] != "en-IN":
                script_reminder = {
                    "ta-IN": "[Respond in Tamil script தமிழ் only, NOT romanized]",
                    "hi-IN": "[Respond in Hindi script हिंदी only, NOT romanized]",
                    "te-IN": "[Respond in Telugu script తెలుగు only, NOT romanized]"
                }.get(session["language"], "")
                messages.append({"role": "user", "content": f"{script_reminder}\n\nUser: {transcript}"})
            else:
                messages.append({"role": "user", "content": transcript})
            
            # ⚡ STREAMING LLM + TTS PIPELINE
            streaming_llm = get_streaming_llm()
            
            response = ""
            sentence_buffer = ""
            first_audio_time = None
            first_token_time = None
            tts_started = False
            
            # ⚡ PERSISTENT TTS: Reuse connection across turns (saves ~1-1.5s)
            tts = await get_or_create_tts(session, session["language"])
            if not tts:
                print("❌ Failed to establish TTS connection")
                session["is_processing"] = False
                return
            
            session["is_speaking"] = True
            # Clear buffers before TTS to prevent echo capture
            session["accumulated_audio"] = b""
            session["silence_detector"].reset()
            session["audio_buffer"].clear()
            
            # Stream LLM response with sentence-level TTS
            sentence_enders = re.compile(r'[.!?।॥။।\n]')
            
            async for token in streaming_llm.stream(messages, max_tokens=150):
                if session.get("call_ended", False):
                    break
                
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                
                response += token
                sentence_buffer += token
                
                # Check for sentence boundary
                if sentence_enders.search(token):
                    sentence = sentence_buffer.strip()
                    
                    # Clean LLM response - remove any "User:" prefixes (LLM hallucination)
                    if sentence.startswith('User:') or sentence.startswith('user:'):
                        sentence = sentence.split(':', 1)[1].strip() if ':' in sentence else sentence
                    # Skip sentences that look like echoed user input
                    if sentence.lower().startswith(('user:', 'உசர்:', 'பயனர்:')):
                        sentence_buffer = ""
                        continue
                    
                    if sentence and len(sentence) > 5:
                        await tts.send_text(sentence)
                        await tts.flush()
                        
                        # Stream audio chunks to Twilio
                        async for audio_chunk in tts.receive_stream():
                            if session.get("call_ended", False):
                                break
                            
                            if first_audio_time is None:
                                first_audio_time = time.perf_counter()
                            
                            mulaw_audio = mp3_to_mulaw(audio_chunk)
                            if mulaw_audio:
                                audio_b64 = base64.b64encode(mulaw_audio).decode()
                                try:
                                    await websocket.send_json({
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": audio_b64}
                                    })
                                except:
                                    pass
                        
                        tts_started = True
                        sentence_buffer = ""
            
            # Send any remaining text
            if sentence_buffer.strip():
                await tts.send_text(sentence_buffer.strip())
                await tts.flush()
                async for audio_chunk in tts.receive_stream():
                    if session.get("call_ended", False):
                        break
                    if first_audio_time is None:
                        first_audio_time = time.perf_counter()
                    mulaw_audio = mp3_to_mulaw(audio_chunk)
                    if mulaw_audio:
                        audio_b64 = base64.b64encode(mulaw_audio).decode()
                        try:
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_b64}
                            })
                        except:
                            pass
            
            llm_time = time.perf_counter() - timing["llm_start"]
            
            # Send mark to track playback
            mark_id = f"response_{int(time.time())}"
            try:
                await websocket.send_json({
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": mark_id}
                })
            except:
                pass
            
            # Set playback_end_time - mark event will clear when done
            estimated_playback = max(0.5, len(response) * 0.08)
            session["playback_end_time"] = time.time() + estimated_playback
            
            # Update conversation history
            session["conversation"].append({"role": "user", "content": transcript})
            session["conversation"].append({"role": "assistant", "content": response})
            
            # Check if we should end the call
            if should_end_call_after_response(response, transcript, session):
                session["call_should_end"] = True
            
            # Compact timing log
            first_audio_latency_ms = (first_audio_time - timing["turn_start"]) * 1000 if first_audio_time else 0
            print(f"✅ Response: {response[:60]}... | Latency: {first_audio_latency_ms:.0f}ms")
            
            # Auto-end call if needed
            if session.get("call_should_end", False):
                print("📞 Ending call automatically")
                # Wait for farewell TTS to finish playing (typically 3-4 seconds)
                await asyncio.sleep(4.0)
                session["call_ended"] = True
                try:
                    await websocket.close()
                    print("✔️ Call ended automatically")
                except Exception as e:
                    print(f"ℹ️ WebSocket already closed: {e}")
                return
        
        except Exception as e:
            print(f"❌ Error in process_speech_streaming: {e}")
            import traceback
            traceback.print_exc()
        finally:
            session["is_processing"] = False
            # Safety: Reset is_speaking if no mark event arrives (error recovery)
            # The mark event will also set this to False when playback completes
            if not session.get("playback_end_time"):
                session["is_speaking"] = False


async def process_speech(
    websocket: WebSocket,
    stream_sid: str,
    session: dict,
    timing: dict
):
    """
    Process user speech: STT → LLM → TTS
    This is the core real-time pipeline.
    Uses lock to prevent concurrent WebSocket access.
    """
    # 🛑 Check if call has ended before processing
    if session.get("call_ended", False):
        print("🛑 Call ended - skipping speech processing")
        return
    
    # ⚡ Use lock to prevent concurrent access to STT/TTS WebSockets
    lock = session.get("processing_lock")
    if lock and lock.locked():
        print("⏭️ Skipping - another turn is still processing")
        return
    
    async with lock if lock else asyncio.Lock():
        session["is_processing"] = True
        audio_data = session["accumulated_audio"]
        audio_duration = len(audio_data) / 2 / 16000  # Duration in seconds (16-bit = 2 bytes per sample)
        
        # Clear buffers for next utterance
        session["accumulated_audio"] = b""
        session["silence_detector"].reset()
        session["audio_buffer"].clear()
        
        # 🛑 Check again in case call ended during buffer clearing
        if session.get("call_ended", False):
            print("🛑 Call ended during buffer clear - aborting")
            session["is_processing"] = False
            return
        
        try:
            # ⏱️ STT - Create fresh connection each turn to avoid WebSocket conflicts
            timing["stt_start"] = time.perf_counter()
            
            # ⚡ Always create new STT client to avoid 'recv already waiting' errors
            # Close existing connection first
            if session.get("stt_client"):
                try:
                    await session["stt_client"].disconnect()
                except:
                    pass
            
            # ⚡ Use locked language for STT to get accurate transcription
            # If language is locked, use it; otherwise use 'unknown' for auto-detect
            stt_language = session.get("locked_language") or "unknown"
            print(f"🎯 STT language: {stt_language}")
            
            stt = SarvamRealtimeSTT(language=stt_language)
            session["stt_client"] = stt
            if not await stt.connect(max_retries=2, retry_delay=0.1):
                print("❌ Failed to establish STT connection")
                session["is_processing"] = False
                return
            
            transcript = ""
            
            # ⚡ Trim leading/trailing silence to help STT focus on speech
            # This reduces processing time and avoids STT getting confused by long silences
            trimmed_audio = trim_silence(audio_data, threshold=300, padding_ms=300)
            trimmed_duration = len(trimmed_audio) / 2 / 16000
            
            # Convert trimmed PCM to proper WAV format
            wav_audio = pcm_to_wav(trimmed_audio, sample_rate=16000, sample_width=2)
            print(f"📦 Audio: {audio_duration:.1f}s → {trimmed_duration:.1f}s (trimmed), WAV: {len(wav_audio)} bytes")
        
            await stt.send_audio(wav_audio, encoding="audio/wav")
            await stt.flush()
        
            # Receive with timeout to prevent hanging
            try:
                result = await asyncio.wait_for(stt.receive(), timeout=6.0)
                if result:
                    transcript = result.transcript
                    detected_language = result.language_code or "en-IN"
                    
                    # --- SMART LANGUAGE DETECTION ---
                    # 1. Check if user explicitly requests language switch (ALWAYS check, even if locked)
                    requested_lang = detect_language_switch_request(transcript)
                    if requested_lang:
                        # User explicitly requested a language - this OVERRIDES any existing lock
                        if session["locked_language"] and requested_lang != session["locked_language"]:
                            print(f"🔓 UNLOCKING from {session['locked_language']} → switching to {requested_lang}")
                        session["locked_language"] = requested_lang
                        session["language"] = requested_lang
                        session["language_streak"] = 0
                        print(f"🔒 Language LOCKED to: {requested_lang} (user explicit request)")
                    elif session["locked_language"]:
                        # Use locked language, don't switch
                        session["language"] = session["locked_language"]
                        print(f"🔒 Using LOCKED language: {session['locked_language']}")
                    else:
                        # 2. Detect language from script characters in transcript
                        script_lang, script_chars = detect_script_language(transcript)
                        
                        if script_lang and script_chars >= 3:
                            # Indian script detected in transcript - map to supported language
                            # Map non-supported scripts to closest supported language
                            lang_mapping = {
                                'ta-IN': 'ta-IN',
                                'hi-IN': 'hi-IN',
                                'te-IN': 'te-IN',
                                'kn-IN': 'ta-IN',   # Kannada -> use Tamil (South Indian)
                                'ml-IN': 'ta-IN',   # Malayalam -> use Tamil (South Indian)
                                'bn-IN': 'hi-IN',   # Bengali -> use Hindi (similar region)
                                'pa-IN': 'hi-IN',   # Punjabi -> use Hindi (North Indian)
                                'mr-IN': 'hi-IN',   # Marathi -> use Hindi (uses Devanagari)
                                'gu-IN': 'hi-IN',   # Gujarati -> use Hindi
                                'or-IN': 'hi-IN',   # Odia -> use Hindi
                            }
                            mapped_lang = lang_mapping.get(script_lang, 'en-IN')
                            
                            if mapped_lang != session["language"]:
                                session["language"] = mapped_lang
                                session["language_streak"] = 1
                                print(f"🌐 Language switched to: {mapped_lang} (script detected: {script_lang}, chars: {script_chars})")
                            else:
                                print(f"🌐 Confirmed language: {session['language']} (script: {script_lang})")
                        else:
                            # 3. Use STT detected language with reduced streak requirement
                            # Map STT language to supported language
                            stt_lang_mapping = {
                                'ta-IN': 'ta-IN',
                                'hi-IN': 'hi-IN', 
                                'te-IN': 'te-IN',
                                'kn-IN': 'ta-IN',
                                'ml-IN': 'ta-IN',
                                'bn-IN': 'hi-IN',
                                'pa-IN': 'hi-IN',
                                'mr-IN': 'hi-IN',
                                'gu-IN': 'hi-IN',
                                'or-IN': 'hi-IN',
                                'en-IN': 'en-IN',
                            }
                            mapped_stt_lang = stt_lang_mapping.get(detected_language, 'en-IN')
                            
                            if mapped_stt_lang == session.get("last_detected_lang"):
                                session["language_streak"] += 1
                            else:
                                session["language_streak"] = 1
                            
                            session["last_detected_lang"] = mapped_stt_lang
                            
                            # Switch on first detection of non-English, or 2+ for switching between languages
                            if mapped_stt_lang != 'en-IN' and session["language"] == 'en-IN':
                                # First non-English detection - switch immediately
                                session["language"] = mapped_stt_lang
                                print(f"🌐 Language switched to: {mapped_stt_lang} (STT detected non-English)")
                            elif session["language_streak"] >= 2 and mapped_stt_lang != session["language"]:
                                session["language"] = mapped_stt_lang
                                # Auto-lock after consistent detection
                                if not session.get("locked_language"):
                                    session["locked_language"] = mapped_stt_lang
                                    print(f"🔒 Auto-locked language to: {mapped_stt_lang} (consistent detection)")
                                print(f"🌐 Language switched to: {mapped_stt_lang} (streak: {session['language_streak']})")
                            else:
                                # Auto-lock on first turn if detection matches default
                                if session["turn_count"] == 1 and mapped_stt_lang == session["language"] and not session.get("explicit_lock"):
                                    session["locked_language"] = mapped_stt_lang
                                    print(f"🔒 Auto-locked language to: {mapped_stt_lang} (first turn confirmation)")
                                print(f"🌐 Keeping language: {session['language']} (STT: {detected_language} -> {mapped_stt_lang})")
                    
            except asyncio.TimeoutError:
                print("⏰ STT receive timeout - no response after 10s")
                transcript = ""
                # Reset connection on timeout
                session["stt_client"] = None
            
            # 🛑 Check if call ended during STT
            if session.get("call_ended", False):
                print("🛑 Call ended during STT - aborting")
                session["is_processing"] = False
                return
            
            stt_time = time.perf_counter() - timing["stt_start"]
            print(f"🎤 STT ({stt_time:.2f}s): '{transcript}' [lang: {session['language']}]")
            
            # Handle empty or very short transcript (likely noise or timeout)
            if not transcript.strip() or len(transcript.strip()) < 2:
                print("⚠️ Empty transcript - asking user to repeat")
                
                # 🛑 Check if call ended - don't try to respond
                if session.get("call_ended", False):
                    print("🛑 Call ended - skipping repeat request")
                    session["is_processing"] = False
                    return
                
                session["garbled_count"] += 1
                
                # If we've had 3+ garbled/empty transcripts, offer language selection
                if session["garbled_count"] >= 3:
                    session["garbled_count"] = 0  # Reset counter
                    print("🌐 Multiple garbled transcripts - offering language selection")
                    lang_prompt = "I'm having trouble understanding. Would you like to speak in Tamil, Hindi, or continue in English? Tamil, Hindi, ya English?"
                    await send_tts_response(websocket, stream_sid, lang_prompt, session, "en-IN")
                else:
                    # Ask user to repeat in their language
                    repeat_messages = {
                        'en-IN': "Sorry, I couldn't hear that clearly. Could you please repeat?",
                        'ta-IN': "மன்னிக்கவும், சரியா கேக்கல. மீண்டும் சொல்ல முடியுமா?",
                        'hi-IN': "माफ़ कीजिए, मुझे सुनाई नहीं दिया। कृपया फिर से बोलिए?",
                        'te-IN': "క్షమించండి, నాకు వినిపించలేదు. దయచేసి మళ్ళీ చెప్పగలరా?"
                    }
                    repeat_msg = repeat_messages.get(session["language"], repeat_messages['en-IN'])
                    await send_tts_response(websocket, stream_sid, repeat_msg, session, session["language"])
                session["is_processing"] = False
                return
            
            # Check if transcript looks garbled (non-Latin script but very short/nonsensical)
            script_lang, script_chars = detect_script_language(transcript)
            is_garbled = False
            
            # Consider it garbled if:
            # 1. Has Indian script but user asked for English
            # 2. Or has script from wrong language than what we expect
            # 3. Or very short non-English transcript (likely noise)
            if script_chars > 0 and script_chars < 10:
                # Short non-English text - might be garbled
                words_in_transcript = len(transcript.split())
                if words_in_transcript <= 2:
                    is_garbled = True
                    print(f"⚠️ Possibly garbled transcript: '{transcript}' (script: {script_lang}, chars: {script_chars})")
            
            if is_garbled:
                session["garbled_count"] += 1
                
                if session["garbled_count"] >= 3:
                    session["garbled_count"] = 0
                    print("🌐 Multiple garbled transcripts - offering language selection")
                    lang_prompt = "I'm having trouble understanding. Would you like to speak in Tamil, Hindi, or continue in English? Tamil, Hindi, ya English?"
                    await send_tts_response(websocket, stream_sid, lang_prompt, session, "en-IN")
                    session["is_processing"] = False
                    return
            else:
                # Good transcript - reset garbled counter
                session["garbled_count"] = 0
            
            # Check if user is saying goodbye (check ALL languages since STT may misdetect)
            # Only end call if user CLEARLY says goodbye - not contextual rejections
            if detect_goodbye(transcript, session["language"]):
                # Double-check: Only end if we have minimum booking info OR user explicitly wants to leave
                has_min_info = len(session["conversation"]) >= 6  # At least 3 turns
                explicit_bye_words = ['bye', 'goodbye', 'பை', 'बाय', 'బై', 'alvida', 'போறேன்', 'चलता', 'వెళ్తా']
                is_explicit_bye = any(word in transcript.lower() for word in explicit_bye_words)
                
                if is_explicit_bye or has_min_info:
                    print(f"👋 User said goodbye: '{transcript}' - ending call gracefully")
                    
                    # Use locked language for goodbye, fallback to session language
                    goodbye_lang = session.get("locked_language") or session["language"]
                    hosp_name = getattr(config, 'HOSPITAL_NAME', 'Anurag AI Hospital')
                    
                    # Send final goodbye message - Natural, warm phrases for each language
                    goodbye_messages = {
                        'en-IN': f'Thank you for calling {hosp_name}. Take care and have a great day!',
                        'ta-IN': f'{hosp_name}-க்கு கால் பண்ணதுக்கு ரொம்ப நன்றி. உங்களை சந்திப்போம்!',
                        'hi-IN': f'{hosp_name} को कॉल करने के लिए धन्यवाद। अपना ख्याल रखिए!',
                        'te-IN': f'{hosp_name}కు కాల్ చేసినందుకు చాలా ధన్యవాదాలు. మిమ్మల్ని కలుస్తాం!'
                    }
                    
                    goodbye_text = goodbye_messages.get(goodbye_lang, goodbye_messages['en-IN'])
                    await send_tts_response(websocket, stream_sid, goodbye_text, session, goodbye_lang)
                    
                    # Wait for TTS to complete then end call
                    await asyncio.sleep(3)
                    session["call_ended"] = True
                    session["is_processing"] = False
                    
                    # Close WebSocket gracefully
                    try:
                        await websocket.close()
                        print("✔️ WebSocket closed - call terminated")
                    except Exception as e:
                        print(f"ℹ️ WebSocket already closed: {e}")
                    return
                else:
                    print(f"ℹ️ Possible goodbye detected but conversation too short, continuing...")
            
            # 🛑 Check if call ended before LLM
            if session.get("call_ended", False):
                print("🛑 Call ended before LLM - aborting")
                session["is_processing"] = False
                return
            
            # ⏱️ LLM + TTS (STREAMING PIPELINE)
            timing["llm_start"] = time.perf_counter()
            
            # Get language name for LLM
            lang_names = {
                'en-IN': 'English', 'hi-IN': 'Hindi',
                'ta-IN': 'Tamil', 'te-IN': 'Telugu'
            }
            lang_name = lang_names.get(session["language"], "English")
            
            # Prepare system prompt with language
            current_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %B %d, %Y")
            
            # Add native script enforcement prefix for non-English
            script_prefix = ""
            if session["language"] == "ta-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Tamil script (தமிழ்). NEVER use Roman/English letters for Tamil words. Example: Write 'சரி, உங்க பேரு என்ன?' NOT 'Sari, unga peru enna?'\n\n"
            elif session["language"] == "hi-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Hindi script (हिंदी). NEVER use Roman/English letters for Hindi words. Example: Write 'ठीक है, आपका नाम क्या है?' NOT 'Theek hai, aapka naam kya hai?'\n\n"
            elif session["language"] == "te-IN":
                script_prefix = "⚠️ CRITICAL: You MUST respond ONLY in Telugu script (తెలుగు). NEVER use Roman/English letters for Telugu words.\n\n"
            
            system_prompt = script_prefix + config.HOSPITAL_SYSTEM_PROMPT.format(
                user_language=lang_name,
                current_date=current_date
            )
            
            # ============================================================
            # 🔍 SMART RAG - Check if query needs knowledge base
            # ============================================================
            rag_context = None
            use_rag = should_use_rag(transcript, session["language"])
            
            if use_rag and getattr(config, 'FILE_SEARCH_ENABLED', False):
                try:
                    from modules.google_file_search import get_relevant_context
                    print(f"🔍 RAG triggered for: '{transcript[:50]}...'")
                    
                    rag_start = time.perf_counter()
                    # Pass session language so Gemini responds in Tamil/Hindi/Telugu directly
                    rag_context = get_relevant_context(
                        query=transcript,
                        max_chars=1000,  # Keep context small for speed
                        response_language=session["language"]  # e.g., 'ta-IN' for Tamil
                    )
                    rag_time = time.perf_counter() - rag_start
                    
                    if rag_context:
                        print(f"✅ RAG context retrieved ({len(rag_context)} chars) in {rag_time:.2f}s")
                        print(f"   📝 RAG Response: {rag_context[:100]}...")
                    else:
                        print(f"⚠️ No relevant RAG context found ({rag_time:.2f}s)")
                except Exception as e:
                    print(f"⚠️ RAG error (continuing without context): {e}")
            
            # Add RAG context to system prompt if available
            if rag_context:
                system_prompt += f"""

📚 KNOWLEDGE BASE INFORMATION (use this to answer the user's question):
---
{rag_context}
---
Use the above information to provide accurate answers about pricing, timings, doctors, etc.
"""
            
            # Build messages for streaming LLM
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(session["conversation"][-20:])  # Last 20 messages
            
            # Add explicit language reminder for non-English languages
            if session["language"] != "en-IN":
                script_reminder = {
                    "ta-IN": "[Respond in Tamil script தமிழ் only, NOT romanized]",
                    "hi-IN": "[Respond in Hindi script हिंदी only, NOT romanized]",
                    "te-IN": "[Respond in Telugu script తెలుగు only, NOT romanized]"
                }.get(session["language"], "")
                messages.append({"role": "user", "content": f"{script_reminder}\n\nUser: {transcript}"})
            else:
                messages.append({"role": "user", "content": transcript})
            
            # ⚡ STREAMING LLM + TTS PIPELINE
            # Start TTS as soon as we have enough text
            streaming_llm = get_streaming_llm()
            
            response = ""
            first_token_time = None
            sentence_buffer = ""
            sentences_sent = 0
            
            # Detect TTS language from session
            tts_language = session["language"]
            
            # TTS speaker mapping
            speakers = {
                'en-IN': 'anushka', 'hi-IN': 'manisha',
                'ta-IN': 'anushka', 'te-IN': 'anushka'
            }
            speaker = speakers.get(tts_language, 'anushka')
            
            # Prepare TTS connection
            session["is_speaking"] = True
            session["accumulated_audio"] = b""
            session["silence_detector"].reset()
            session["audio_buffer"].clear()
            
            # ⚡ PERSISTENT TTS: Reuse connection across turns (saves ~1-1.5s)
            tts = await get_or_create_tts(session, tts_language, speaker)
            
            if not tts:
                print("❌ TTS connection failed for streaming")
                # Fallback to non-streaming (with RAG if enabled)
                if use_rag and getattr(config, 'FILE_SEARCH_ENABLED', False):
                    response = get_chatbot_response_with_file_search(
                        query=transcript,
                        user_phone=session.get("caller", ""),
                        history=session["conversation"],
                        user_language=session["language"],
                        user_language_name=lang_name,
                        use_file_search=True
                    )
                else:
                    response = get_chatbot_response(
                        query=transcript,
                        user_phone=session.get("caller", ""),
                        history=session["conversation"],
                        user_language=session["language"],
                        user_language_name=lang_name
                    )
                await send_tts_response(websocket, stream_sid, response, session, tts_language)
                session["is_processing"] = False
                return
            
            total_audio_bytes = 0
            timing["tts_start"] = time.perf_counter()
            first_audio_time = None
            
            # Sentence-ending patterns
            import re
            sentence_end_pattern = re.compile(r'[.!?।॥]')
            
            print(f"⚡ Starting streaming LLM+TTS pipeline...")
            
            async for token in streaming_llm.stream(messages, max_tokens=150, temperature=0.3):  # Tamil needs more tokens
                if not first_token_time:
                    first_token_time = time.perf_counter()
                    llm_first_token_ms = (first_token_time - timing["llm_start"]) * 1000
                    print(f"⚡ First LLM token: {llm_first_token_ms:.0f}ms")
                
                response += token
                sentence_buffer += token
                
                # Check for sentence end
                if sentence_end_pattern.search(sentence_buffer) and len(sentence_buffer) >= 15:
                    # Send this sentence to TTS immediately
                    sentences_sent += 1
                    sentence_to_speak = sentence_buffer.strip()
                    sentence_buffer = ""
                    
                    # Clean LLM response - remove any "User:" prefixes (LLM hallucination)
                    if sentence_to_speak.startswith('User:') or sentence_to_speak.startswith('user:'):
                        sentence_to_speak = sentence_to_speak.split(':', 1)[1].strip() if ':' in sentence_to_speak else sentence_to_speak
                    # Skip sentences that look like echoed user input
                    if sentence_to_speak.lower().startswith(('user:', 'உசர்:', 'பயனர்:')):
                        continue
                    
                    if sentences_sent == 1:
                        print(f"🔊 First sentence ready: '{sentence_to_speak[:50]}...'")
                    
                    # Send to TTS
                    await tts.send_text(sentence_to_speak + " ")
                    await tts.flush()
                    
                    # Stream audio chunks to Twilio
                    async for audio_chunk in tts.receive_stream():
                        if not first_audio_time:
                            first_audio_time = time.perf_counter()
                            first_audio_ms = (first_audio_time - timing["llm_start"]) * 1000
                            print(f"⚡ First audio chunk: {first_audio_ms:.0f}ms from LLM start")
                        
                        # Convert and send to Twilio
                        mulaw_audio = mp3_to_mulaw(audio_chunk)
                        if mulaw_audio:
                            total_audio_bytes += len(mulaw_audio)
                            media_message = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_to_base64(mulaw_audio)}
                            }
                            try:
                                await websocket.send_json(media_message)
                            except:
                                break
            
            # Send any remaining text
            if sentence_buffer.strip():
                await tts.send_text(sentence_buffer.strip())
                await tts.flush()
                async for audio_chunk in tts.receive_stream():
                    mulaw_audio = mp3_to_mulaw(audio_chunk)
                    if mulaw_audio:
                        total_audio_bytes += len(mulaw_audio)
                        media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_to_base64(mulaw_audio)}
                        }
                        try:
                            await websocket.send_json(media_message)
                        except:
                            break
            
            # Send playback mark
            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": f"response_{int(time.time())}"}
            }
            try:
                await websocket.send_json(mark_message)
            except:
                pass
            
            llm_time = time.perf_counter() - timing["llm_start"]
            tts_time = time.perf_counter() - timing["tts_start"]
            
            print(f"🤖 LLM ({llm_time:.2f}s): '{response[:100]}...'")
            
            # Wait for playback
            if total_audio_bytes > 0:
                playback_duration = total_audio_bytes / 8000
                # Set end time so we can start listening early in last 1s
                session["playback_end_time"] = time.time() + playback_duration
                print(f"⏳ Waiting {playback_duration:.1f}s for playback")
                await asyncio.sleep(playback_duration + 0.1)
            
            # Re-enable listening
            session["is_speaking"] = False
            print(f"🎤 Ready to listen (is_speaking=False)")
            
            # Update conversation history with better context preservation
            session["conversation"].append({"role": "user", "content": transcript})
            session["conversation"].append({"role": "assistant", "content": response})
            
            # Check if we should end the call after this response
            if should_end_call_after_response(response, transcript, session):
                session["call_should_end"] = True
            
            # ⏱️ Total timing (TTS was done inline with streaming LLM above)
            total_time = time.perf_counter() - timing["turn_start"]
            
            # Calculate first audio latency (perceived latency by user)
            first_audio_latency_ms = 0
            if first_audio_time:
                first_audio_latency_ms = (first_audio_time - timing["turn_start"]) * 1000
            
            print(f"\n{'='*50}")
            print(f"⚡ TURN {session['turn_count']} - STREAMING PIPELINE")
            print(f"{'='*50}")
            print(f"🎤 STT:              {stt_time:.2f}s")
            if first_token_time:
                print(f"🧠 LLM First Token:  {(first_token_time - timing['llm_start'])*1000:.0f}ms")
            print(f"🧠 LLM+TTS Total:    {llm_time:.2f}s")
            print(f"🔊 First Audio:      {first_audio_latency_ms:.0f}ms (from turn start)")
            print(f"{'─'*50}")
            print(f"⏱ TOTAL:            {total_time:.2f}s")
            print(f"⚡ USER PERCEIVED:   {first_audio_latency_ms:.0f}ms")
            print(f"{'='*50}\n")
            
            # Auto-end call if booking is complete and user acknowledged
            if session.get("call_should_end", False):
                print("📞 Booking complete - ending call automatically")
                # LLM already said goodbye in response, no duplicate message needed
                await asyncio.sleep(0.5)  # Brief wait for final TTS
                session["call_ended"] = True
                
                try:
                    await websocket.close()
                    print("✔️ Call ended automatically after booking confirmation")
                except Exception as e:
                    print(f"ℹ️ WebSocket already closed: {e}")
                return
        
        except Exception as e:
            print(f"❌ Error in process_speech: {e}")
            import traceback
            traceback.print_exc()
        finally:
            session["is_processing"] = False
            # Safety: Reset is_speaking if no mark event arrives (error recovery)
            if not session.get("playback_end_time"):
                session["is_speaking"] = False


async def send_tts_response(
    websocket: WebSocket,
    stream_sid: str,
    text: str,
    session: dict,
    language: str = "en-IN"
):
    """
    Send TTS audio back to Twilio via Media Stream.
    Uses Sarvam streaming TTS (MP3 output), converts to mulaw for Twilio.
    ⚡ OPTIMIZED: Uses persistent TTS connection when possible.
    """
    # Check if call has ended before doing anything
    if session.get("call_ended", False):
        return
    
    # Map language to speaker (Sarvam TTS voices)
    speakers = {
        'en-IN': 'anushka',
        'hi-IN': 'manisha',
        'ta-IN': 'anushka',
        'te-IN': 'anushka',
        'kn-IN': 'anushka',
        'ml-IN': 'anushka',
        'mr-IN': 'anushka',
        'gu-IN': 'anushka',
        'bn-IN': 'anushka'
    }
    speaker = speakers.get(language, 'anushka')
    
    # Set speaking flag to prevent barge-in
    session["is_speaking"] = True
    session["_speaking_start"] = time.time()  # Track when speaking started for timeout
    
    # Clear any accumulated audio
    session["accumulated_audio"] = b""
    session["silence_detector"].reset()
    session["audio_buffer"].clear()
    
    # Track total audio bytes sent
    total_audio_bytes = 0
    
    # Reuse TTS connection
    tts = await get_or_create_tts(session, language, speaker)
    
    try:
        if not tts:
            session["is_speaking"] = False
            return
        
        # ⚡ NORMALIZE TEXT for better TTS pronunciation
        # Fixes: times (4:00 → 4 மணி), abbreviations (RK → ஆர் கே), etc.
        normalized_text = normalize_text_for_tts(text, language)
        if normalized_text != text:
            print(f"📝 TTS normalized: '{text[:50]}...' → '{normalized_text[:50]}...'")
        
        await tts.send_text(normalized_text)
        await tts.flush()
        
        # Stream audio to Twilio
        async for audio_chunk in tts.receive_stream():
            mulaw_audio = mp3_to_mulaw(audio_chunk)
            
            if mulaw_audio:
                if session.get("call_ended", False):
                    break
                    
                # Send to Twilio
                try:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_to_base64(mulaw_audio)}
                    })
                    total_audio_bytes += len(mulaw_audio)
                except:
                    break
        
        # Send mark for playback tracking
        if not session.get("call_ended", False):
            try:
                await websocket.send_json({
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": f"response_{int(time.time())}"}
                })
            except:
                pass
        
        # Set playback_end_time - mark event will clear it
        if total_audio_bytes > 0:
            estimated_duration = total_audio_bytes / 8000
            session["playback_end_time"] = time.time() + estimated_duration
        else:
            # No audio sent, reset immediately
            session["playback_end_time"] = 0
            session["is_speaking"] = False
    
    except Exception as e:
        print(f"❌ TTS error: {e}")
        session["tts_client"] = None
        session["playback_end_time"] = 0
        session["is_speaking"] = False


@app.get('/make_call')
@app.post('/make_call')
async def make_call(request: Request):
    """Initiate outbound call with Media Streams."""
    from twilio.rest import Client
    
    # Get phone number from query params or form data
    phone_number = request.query_params.get('phone_number')
    if not phone_number:
        form = await request.form()
        phone_number = form.get('phone_number')
    
    if not phone_number:
        return JSONResponse(
            status_code=400,
            content={"error": "phone_number is required"}
        )
    
    try:
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        
        call = client.calls.create(
            to=phone_number,
            from_=config.TWILIO_PHONE_NUMBER,
            url=f"https://{config.NGROK_URL}/outbound_voice",
            status_callback=f"https://{config.NGROK_URL}/call_status",
            status_callback_event=['initiated', 'ringing', 'answered', 'completed']
        )
        
        print(f"📞 Call initiated: {call.sid}")
        
        return {
            "message": f"Call initiated to {phone_number}",
            "call_sid": call.sid,
            "status": call.status
        }
        
    except Exception as e:
        print(f"❌ Error initiating call: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post('/call_status')
async def call_status(request: Request):
    """
    General Twilio call status callback.
    Note: Scheduled calls use /scheduled_call_status/{schedule_id} instead.
    """
    form = await request.form()
    call_sid = form.get("CallSid")
    status = form.get("CallStatus")
    
    # Log call status for debugging
    print(f"📞 Call status update - SID: {call_sid}, Status: {status}")

    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("🚀 REAL-TIME VOICE AGENT")
    print("=" * 60)
    print("Mode: Twilio Media Streams + Sarvam WebSocket APIs")
    print("Target: Sub-1-second latency")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=7000)