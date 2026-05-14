import os
from src.db import create_complaint, upsert_call

try:
    # Ensure there is a call to attach to (foreign key constraint)
    upsert_call(
        call_sid="test_call_sid",
        phone_number="0112345678",
    )
    
    res = create_complaint(
        call_sid="test_call_sid",
        caller_name="Test User",
        contact_number="0112345678",
        service_category="Waste Management",
        specific_service="Garbage not collected",
        description="Garbage not collected for 2 weeks",
        location_address="Colombo 7"
    )
    print("Success:", res)
except Exception as e:
    import traceback
    traceback.print_exc()
