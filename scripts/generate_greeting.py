#!/usr/bin/env python
# scripts/generate_greeting.py
"""
One-off script: synthesize the trilingual opening greeting and save it
to static/greeting.mp3.  Run this once before starting the server.

Usage:
    uv run python scripts/generate_greeting.py
"""

import sys
from pathlib import Path

# Make sure we can import from src/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.language import OPENING_GREETING
from src.tts import synthesize

OUTPUT_PATH = ROOT / "static" / "greeting.mp3"
OUTPUT_PATH.parent.mkdir(exist_ok=True)

def main() -> None:
    print("🎙  Synthesizing trilingual greeting …")
    print(f"   Text: {OPENING_GREETING[:80]}…")

    # Use English voice for the combined trilingual greeting
    audio_bytes = synthesize(OPENING_GREETING, lang="en", use_ssml=False)

    OUTPUT_PATH.write_bytes(audio_bytes)
    print(f"✅  Saved {len(audio_bytes):,} bytes → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
