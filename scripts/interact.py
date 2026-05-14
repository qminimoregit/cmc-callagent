#!/usr/bin/env python
# scripts/interact.py
"""
CLI tool to test the Nimali agent's brain and voice locally.
Type a message, see the detection/reply, and get an MP3 output.
"""

import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.llm import chat
from src.tts import synthesize

def main():
    print("🇱🇰  Nimali Interaction Tester")
    print("   (Type 'exit' or 'quit' to stop)\n")
    
    history = []
    
    while True:
        try:
            user_input = input("You: ")
            if user_input.lower() in ("exit", "quit"):
                break
            
            if not user_input.strip():
                continue

            print("🤖 Processing...")
            
            # 1. LLM Step (Brain)
            reply_text, updated_history, lang, should_escalate = chat(user_input, history)
            history = updated_history
            
            print(f"\n--- Result ---")
            print(f"Detected Language: {lang.upper()}")
            print(f"Nimali: {reply_text}")
            if should_escalate:
                print("⚠️  [System would escalate this call]")
            
            # 2. TTS Step (Voice)
            print("🔊 Synthesizing audio...")
            audio_bytes = synthesize(reply_text, lang)
            
            output_file = ROOT / "test_reply.mp3"
            output_file.write_bytes(audio_bytes)
            
            print(f"✅ Audio saved to: {output_file}")
            print(f"   (Open this file to hear Nimali's response)\n")
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
