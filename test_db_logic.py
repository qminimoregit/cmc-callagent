import os
from dotenv import load_dotenv
load_dotenv()
from src.db import create_complaint, list_complaints

try:
    create_complaint("call_test_123", "Test Category", "Test Service", "Test Desc", "123 Test St", "John", "0770000000")
    print("Complaint created.")
    complaints = list_complaints()
    print("Complaints:", complaints)
except Exception as e:
    print("Error:", e)
