#!/usr/bin/env python
# scripts/voice_talk.py
"""
Voice-to-voice CLI tool. 
Records your voice, processes it through Nimali, and speaks back to you.
"""

import sys
import os
import time
import io
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import sounddevice as sd
import numpy as np
import soundfile as sf
from src.stt import transcribe
from src.llm import chat
from src.tts import synthesize

# Audio recording settings
SAMPLERATE = 16000  # 16 kHz for mic input — far better English STT accuracy
DURATION = 6        # 6 seconds gives enough time for full sentences

def main():
    print("🎙  Nimali Voice Talk")
    print("   (Listening for 5 seconds at a time. Press Ctrl+C to stop.)\n")
    
    history = []
    
    while True:
        try:
            print("👂 Listening... Speak now!")
            # Record audio
            recording = sd.rec(int(DURATION * SAMPLERATE), samplerate=SAMPLERATE, channels=1, dtype='int16')
            sd.wait()  # Wait until recording is finished
            
            # Check if the recording is completely silent (permission issue)
            if np.max(np.abs(recording)) == 0:
                print("⚠️  Warning: Microphone recorded complete silence. Check macOS System Settings > Privacy & Security > Microphone and ensure your terminal/editor has permission.")
                continue

            # Convert numpy array to raw PCM bytes (LINEAR16) for Google STT
            audio_bytes = recording.tobytes()
            
            print("🧠 Thinking...")
            
            # 1. STT (Speech to Text)
            transcript, stt_lang = transcribe(audio_bytes)
            
            if not transcript:
                print("❓ Didn't catch that. Please try again.")
                continue
                
            print(f"You said: {transcript}")

            # 2. LLM (Brain) — pass stt_lang so speech-detected language wins over keyword guessing
            reply_text, updated_history, lang, should_escalate = chat(transcript, history, stt_lang)
            history = updated_history
            
            print(f"Nimali ({lang.upper()}): {reply_text}")

            # 3. TTS (Text to Speech)
            print("🗣  Speaking...")
            reply_audio = synthesize(reply_text, lang)
            
            # 4. Playback
            # Save to temporary file to read with soundfile
            temp_reply = ROOT / "temp_reply.mp3"
            temp_reply.write_bytes(reply_audio)
            
            # Playback — afplay -r speeds up gTTS Sinhala which is naturally slow
            rate = "1.25" if lang == "si" else "1.1"
            os.system(f"afplay -r {rate} {temp_reply}")
            
            print("-" * 30)
            
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
