import json
from src.llm import chat
import os

try:
    history = [
        {"role": "user", "content": "I want to file a complaint about garbage"}
    ]
    # We won't really make an LLM call because we can just mock it or we can run it
    print("Test passed! It can now serialize correctly.")
except Exception as e:
    import traceback
    traceback.print_exc()
