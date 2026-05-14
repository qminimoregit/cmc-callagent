import sys
import os

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), 'src')))

try:
    from db import create_booking
    print("Successfully imported create_booking from src.db")
    # Try to trigger the dateparser import inside the function
    import dateparser
    print("Successfully imported dateparser directly")
except ImportError as e:
    print(f"Import Error: {e}")
except Exception as e:
    print(f"Error: {e}")
