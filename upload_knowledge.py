"""
Upload Hospital Knowledge Documents to Google File Search

This script uploads hospital documents (PDFs, DOCX, TXT) to Google's 
File Search Store for RAG (Retrieval Augmented Generation).

Usage:
    python upload_knowledge.py                    # Upload all from knowledge_docs/
    python upload_knowledge.py file.pdf           # Upload specific file
    python upload_knowledge.py --list             # List uploaded documents
    python upload_knowledge.py --status           # Check status
    python upload_knowledge.py --test "query"     # Test search

Directory Structure:
    knowledge_docs/
    ├── hospital_info.pdf       # Hospital brochure, services
    ├── doctor_schedule.pdf     # Doctor availability
    ├── pricing.pdf             # Treatment pricing
    ├── faqs.txt                # Frequently asked questions
    └── policies.pdf            # Hospital policies
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from modules.google_file_search import (
    init_google_file_search,
    check_file_search_status,
    get_or_create_file_search_store,
    upload_file_to_store,
    upload_folder,
    search_knowledge_base,
    list_file_search_stores,
    delete_file_search_store
)


# Default knowledge documents folder
KNOWLEDGE_DOCS_FOLDER = Path(__file__).parent / "knowledge_docs"


def print_status():
    """Print current File Search status."""
    print("\n" + "=" * 60)
    print("📊 Google File Search Status")
    print("=" * 60)
    
    status = check_file_search_status()
    
    print(f"\n🔧 Configuration:")
    print(f"   SDK Available: {'✅' if status['sdk_available'] else '❌'}")
    print(f"   API Key Set: {'✅' if status['api_key_set'] else '❌'}")
    print(f"   Initialized: {'✅' if status['initialized'] else '❌'}")
    
    if status['current_store']:
        print(f"   Current Store: {status['current_store']}")
    
    print(f"\n📦 File Search Stores:")
    if status['stores']:
        for store in status['stores']:
            print(f"   • {store['display_name']}")
            print(f"     ID: {store['name']}")
    else:
        print("   No stores found")
    
    if not status['api_key_set']:
        print("\n⚠️ GOOGLE_API_KEY not set!")
        print("   Add it to your .env file:")
        print("   GOOGLE_API_KEY=your_api_key_here")
    
    return status['initialized']


def upload_all_documents():
    """Upload all documents from knowledge_docs folder."""
    print("\n" + "=" * 60)
    print("📤 Upload Knowledge Documents")
    print("=" * 60)
    
    # Create folder if it doesn't exist
    if not KNOWLEDGE_DOCS_FOLDER.exists():
        KNOWLEDGE_DOCS_FOLDER.mkdir(parents=True)
        print(f"\n📁 Created folder: {KNOWLEDGE_DOCS_FOLDER}")
        print("\n⚠️ No documents to upload!")
        print("   Add your hospital documents to this folder:")
        print(f"   {KNOWLEDGE_DOCS_FOLDER}")
        print("\n   Supported formats:")
        print("   • PDF, DOCX, DOC, PPTX, XLSX")
        print("   • TXT, MD, HTML, JSON, XML")
        return
    
    # Check for files
    files = list(KNOWLEDGE_DOCS_FOLDER.glob("*"))
    supported_files = [
        f for f in files 
        if f.suffix.lower() in ['.pdf', '.doc', '.docx', '.txt', '.md', '.html', '.json', '.pptx', '.xlsx']
    ]
    
    if not supported_files:
        print(f"\n⚠️ No supported files found in: {KNOWLEDGE_DOCS_FOLDER}")
        print("   Add PDF, DOCX, TXT, or other supported documents")
        return
    
    print(f"\n📂 Found {len(supported_files)} files:")
    for f in supported_files:
        print(f"   • {f.name} ({f.stat().st_size / 1024:.1f} KB)")
    
    # Get or create store
    print(f"\n📦 Getting/Creating File Search Store...")
    store_name = get_or_create_file_search_store()
    
    if not store_name:
        print("❌ Failed to get/create File Search Store")
        return
    
    # Upload each file
    print(f"\n📤 Uploading files...")
    uploaded = []
    failed = []
    
    for file_path in supported_files:
        result = upload_file_to_store(
            file_path=str(file_path),
            display_name=file_path.name,
            store_name=store_name
        )
        
        if result:
            uploaded.append(file_path.name)
        else:
            failed.append(file_path.name)
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Upload Summary")
    print("=" * 60)
    print(f"   ✅ Uploaded: {len(uploaded)}")
    print(f"   ❌ Failed: {len(failed)}")
    
    if failed:
        print(f"\n   Failed files:")
        for f in failed:
            print(f"   • {f}")


def upload_single_file(file_path: str):
    """Upload a single file."""
    file_path = Path(file_path)
    
    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        return
    
    print(f"\n📤 Uploading: {file_path.name}")
    
    store_name = get_or_create_file_search_store()
    if not store_name:
        print("❌ Failed to get File Search Store")
        return
    
    result = upload_file_to_store(
        file_path=str(file_path),
        display_name=file_path.name,
        store_name=store_name
    )
    
    if result:
        print(f"✅ Successfully uploaded: {file_path.name}")
    else:
        print(f"❌ Failed to upload: {file_path.name}")


def test_search(query: str):
    """Test search with a query."""
    print("\n" + "=" * 60)
    print("🔍 Test Search")
    print("=" * 60)
    
    print(f"\n📝 Query: {query}")
    print("\n⏳ Searching...")
    
    result = search_knowledge_base(query)
    
    if result.get('error'):
        print(f"\n❌ Error: {result['error']}")
        return
    
    print(f"\n✅ Answer:")
    print("-" * 40)
    print(result.get('answer', 'No answer found'))
    print("-" * 40)
    
    if result.get('citations'):
        print(f"\n📚 Citations:")
        for cite in result['citations']:
            print(f"   • {cite.get('source', 'Unknown')}")
    
    print(f"\n⏱️ Search time: {result.get('search_time', 0):.2f}s")


def create_sample_documents():
    """Create sample hospital documents for testing."""
    print("\n" + "=" * 60)
    print("📝 Creating Sample Documents")
    print("=" * 60)
    
    # Create folder
    KNOWLEDGE_DOCS_FOLDER.mkdir(parents=True, exist_ok=True)
    
    # Sample FAQ document
    faq_content = """# Anurag AI Hospital - Frequently Asked Questions (Demo Knowledge Base)

## Hospital Timings
Q: What are the hospital timings?
A: OPD (Out-Patient Department) is open Monday to Saturday, 9:00 AM to 9:00 PM. 
   Emergency services are available 24/7, including nights and holidays (simulated demo setting).

Q: Is the hospital open on Sundays?
A: OPD is closed on Sundays. However, Emergency services are available 24/7.

Q: Can I visit after 9 PM?
A: Yes, for emergencies. Emergency doctors are available round the clock.

## Doctors & Departments
Q: What specialists are available?
A: We have:
   - General Physician: For fever, cold, headache, general checkup
   - Orthopedic Specialist: Bone issues, joint pain, back pain
   - Cardiologist: Heart problems, blood pressure, chest pain
   - Dentist: Dental issues, tooth pain
   - Pediatrician: Children's health
   - ENT Specialist: Ear, nose, throat problems

Q: How can I book an appointment?
A: You can call our hospital or use our voice assistant to book an appointment.
   Just tell us your symptoms and preferred time.

## Pricing
Q: What is the consultation fee?
A: General consultation: ₹300-500
   Specialist consultation: ₹500-800
   Emergency consultation: ₹1000-1500

Q: Do you accept insurance?
A: Yes, we accept major health insurance providers.

## Location
Q: Where is Anurag AI Hospital located?
A: 123 Healthcare Boulevard, Tech City, Anna Nagar, Chennai, Tamil Nadu - 600040 (Demo Facility)
   Landmark: Near Anna Nagar Tower, Opposite City Mall

Q: Is parking available?
A: Yes, free parking is available for patients.

## Services
Q: Do you have ambulance service?
A: Yes, 24/7 ambulance service is available. Call our emergency helpline.

Q: Is inpatient facility available?
A: Yes, we have inpatient rooms and wards for admitted patients.

Q: Do you have a pharmacy?
A: Yes, 24-hour pharmacy is available inside the hospital.
"""
    
    faq_path = KNOWLEDGE_DOCS_FOLDER / "hospital_faqs.txt"
    faq_path.write_text(faq_content, encoding='utf-8')
    print(f"✅ Created: {faq_path.name}")
    
    # Sample pricing document
    pricing_content = """# Anurag AI Hospital - Service Pricing Guide (Demo Data)

## Consultation Fees

### General Consultation
- General Physician: ₹300 - ₹500
- Follow-up visit (within 7 days): ₹200

### Specialist Consultation
- Cardiologist: ₹600 - ₹800
- Orthopedic: ₹500 - ₹700
- ENT Specialist: ₹500 - ₹700
- Pediatrician: ₹400 - ₹600
- Dentist: ₹400 - ₹600

### Emergency Services
- Emergency consultation: ₹1000 - ₹1500
- Night charges (after 9 PM): Additional ₹500

## Diagnostic Tests

### Blood Tests
- Complete Blood Count (CBC): ₹350
- Blood Sugar (Fasting/PP): ₹100 each
- Lipid Profile: ₹600
- Thyroid Profile: ₹800
- Liver Function Test: ₹700
- Kidney Function Test: ₹700

### Imaging
- X-Ray: ₹300 - ₹800 (depends on area)
- Ultrasound: ₹800 - ₹1500
- ECG: ₹300
- Echo: ₹1500 - ₹2000

## Room Charges (per day)
- General Ward: ₹1500
- Semi-Private: ₹3000
- Private Room: ₹5000
- ICU: ₹8000 - ₹15000

## Payment Options
- Cash
- Credit/Debit Cards
- UPI
- Health Insurance (Cashless for empaneled insurers)

Note: All prices are demo values for testing and illustrative purposes. Please confirm at the time of service.
"""
    
    pricing_path = KNOWLEDGE_DOCS_FOLDER / "pricing_guide.txt"
    pricing_path.write_text(pricing_content, encoding='utf-8')
    print(f"✅ Created: {pricing_path.name}")
    
    # Sample doctor schedule
    schedule_content = """# Anurag AI Hospital - Doctor Schedule (Demo Schedule)

## General Physician
- Dr. Ramesh Kumar
  - Monday to Saturday: 9:00 AM - 1:00 PM, 5:00 PM - 8:00 PM
  - Sunday: Emergency only

- Dr. Priya Sharma
  - Monday, Wednesday, Friday: 10:00 AM - 2:00 PM
  - Tuesday, Thursday, Saturday: 4:00 PM - 8:00 PM

## Cardiologist
- Dr. Suresh Reddy (Senior Cardiologist)
  - Tuesday, Thursday, Saturday: 10:00 AM - 1:00 PM
  - Appointments recommended

## Orthopedic Specialist
- Dr. Arun Prakash
  - Monday, Wednesday, Friday: 3:00 PM - 7:00 PM
  - Saturday: 10:00 AM - 2:00 PM

## ENT Specialist
- Dr. Lakshmi Narayanan
  - Monday to Friday: 4:00 PM - 8:00 PM
  - Saturday: 9:00 AM - 12:00 PM

## Pediatrician
- Dr. Kavitha Murthy
  - Monday to Saturday: 9:00 AM - 12:00 PM
  - Evening: 5:00 PM - 7:00 PM (Mon, Wed, Fri only)

## Dentist
- Dr. Vijay Kumar
  - Monday to Saturday: 10:00 AM - 1:00 PM, 3:00 PM - 7:00 PM

## Emergency Doctors
- Available 24/7
- Rotating duty schedule

Note: Doctors' schedules may change due to emergencies. 
Please call ahead to confirm availability for specific doctors.
"""
    
    schedule_path = KNOWLEDGE_DOCS_FOLDER / "doctor_schedule.txt"
    schedule_path.write_text(schedule_content, encoding='utf-8')
    print(f"✅ Created: {schedule_path.name}")
    
    print(f"\n✅ Created {3} sample documents in: {KNOWLEDGE_DOCS_FOLDER}")
    print("\n💡 You can now upload these documents:")
    print("   python upload_knowledge.py")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Upload hospital documents to Google File Search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python upload_knowledge.py                    Upload all from knowledge_docs/
  python upload_knowledge.py file.pdf           Upload specific file
  python upload_knowledge.py --status           Check status
  python upload_knowledge.py --test "query"     Test search
  python upload_knowledge.py --create-samples   Create sample documents
        """
    )
    
    parser.add_argument('file', nargs='?', help='Specific file to upload')
    parser.add_argument('--status', action='store_true', help='Show status')
    parser.add_argument('--list', action='store_true', help='List stores')
    parser.add_argument('--test', type=str, help='Test search with query')
    parser.add_argument('--create-samples', action='store_true', help='Create sample documents')
    parser.add_argument('--delete-store', type=str, help='Delete a store by name')
    
    args = parser.parse_args()
    
    # Initialize
    if not init_google_file_search():
        print("\n❌ Failed to initialize Google File Search")
        print("   Check GOOGLE_API_KEY in .env file")
        return
    
    # Handle commands
    if args.status or args.list:
        print_status()
    elif args.test:
        test_search(args.test)
    elif args.create_samples:
        create_sample_documents()
    elif args.delete_store:
        delete_file_search_store(args.delete_store)
    elif args.file:
        upload_single_file(args.file)
    else:
        # Default: upload all documents
        if not print_status():
            return
        upload_all_documents()


if __name__ == "__main__":
    main()
