# src/tts.py
from __future__ import annotations
"""
Google Cloud Text-to-Speech integration — trilingual voice synthesis.
Voices:
  Sinhala  → si-LK auto-selected (Google Cloud's native Sinhala model)
  Tamil    → ta-IN-Chirp3-HD-Aoede (HD neural, warm female)
  English  → en-US-Journey-F (conversational neural)

Multi-language greeting is synthesised per-segment so each language
sounds native rather than being read by a single TTS engine.
"""

import logging
import re

from typing import Optional
from google.cloud import texttospeech

logger = logging.getLogger(__name__)

# ⚡ Pre-warm TTS client at module level — avoids ~200-500ms cold start per request
_tts_client: Optional[texttospeech.TextToSpeechClient] = None

def _get_tts_client() -> texttospeech.TextToSpeechClient:
    """Lazy-initialise a module-level TextToSpeechClient singleton."""
    global _tts_client
    if _tts_client is None:
        _tts_client = texttospeech.TextToSpeechClient()
        logger.info("TTS client initialised (singleton)")
    return _tts_client

# ---------------------------------------------------------------------------
# Voice selection per language
# ---------------------------------------------------------------------------
VOICES: dict[str, texttospeech.VoiceSelectionParams] = {
    # Sinhala — Google Cloud auto-selects the best available si-LK voice.
    # No explicit voice name so we let the engine pick its native model.
    "si": texttospeech.VoiceSelectionParams(
        language_code="si-LK",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
    ),
    # Tamil — Chirp3-HD is Google's highest-quality neural voice family.
    # Aoede is a warm female voice, suitable for customer support.
    "ta": texttospeech.VoiceSelectionParams(
        language_code="ta-IN",
        name="ta-IN-Chirp3-HD-Aoede",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
    ),
    # English — Journey-F is Google's most conversational neural voice.
    "en": texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Journey-F",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
    ),
}

AUDIO_CONFIG = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MP3,
    speaking_rate=1.0,     # Measured, professional pace for government agent
)

# ⚡ Pre-create Sinhala AudioConfig at module level (Fix 5)
# — eliminates object creation per call
_SINHALA_AUDIO_CONFIG = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MP3,
    speaking_rate=1.05,  # Increased for natural conversational pace
)

# ⚡ Pre-create MULAW AudioConfig for streaming (8kHz MULAW)
_MULAW_AUDIO_CONFIG = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MULAW,
    sample_rate_hertz=8000,
    speaking_rate=1.0,
)

# Sinhala auto-voice, Chirp3-HD, and Journey voices don't support SSML
_NO_SSML_VOICES = {"en-US-Journey-F", "ta-IN-Chirp3-HD-Aoede"}
# Sinhala auto-selected voice also does not reliably support SSML
_NO_SSML_LANGS = {"si"}

# ⚡ Audio cache for short, frequently-repeated phrases (Fix 5)
# Caches common phrases like "are you still there?", goodbye, confirmations
_audio_cache: dict[str, bytes] = {}
_CACHE_MAX_TEXT_LEN = 150  # only cache short phrases


def wrap_ssml(text: str, lang: str) -> str:
    """
    Wrap plain-text reply in SSML with natural 300 ms pauses
    between sentences, making the speech sound more human.

    Handles Sinhala/Tamil danda (।) as well as standard Latin punctuation.
    """
    if lang in ("si", "ta"):
        # Split on standard punctuation AND the Devanagari/Tamil danda
        sentences = re.split(r'(?<=[.!?।])\s+', text)
    else:
        sentences = re.split(r'(?<=[.!?])\s+', text)

    parts = '<break time="300ms"/>'.join(s.strip() for s in sentences if s.strip())
    return f"<speak>{parts}</speak>"


def synthesize(text: str, lang: str, use_ssml: bool = True) -> bytes:
    """
    Convert text to MP3 audio bytes using the correct voice for the language.

    All three languages now use Google Cloud TTS:
      - si: auto-selected si-LK voice (native Sinhala, natural sounding)
      - ta: ta-IN-Chirp3-HD-Aoede (HD neural Tamil)
      - en: en-US-Journey-F (conversational English)

    Parameters
    ----------
    text     : str  — plain text or pre-built SSML string
    lang     : str  — 'si', 'ta', or 'en'
    use_ssml : bool — if True, wraps text in SSML before synthesis
                      (automatically disabled for voices that don't support it)

    Returns
    -------
    bytes : raw MP3 audio content
    """
    # ⚡ Check cache for short, repeated phrases (Fix 5)
    cache_key = f"{lang}:{text[:200]}"
    if len(text) <= _CACHE_MAX_TEXT_LEN and cache_key in _audio_cache:
        logger.info("TTS cache hit → lang=%s  chars=%d", lang, len(text))
        return _audio_cache[cache_key]

    tts_client = _get_tts_client()
    voice = VOICES.get(lang, VOICES["en"])

    # Determine if this voice/language supports SSML
    voice_name = getattr(voice, "name", "") or ""
    voice_uses_ssml = (
        use_ssml
        and voice_name not in _NO_SSML_VOICES
        and lang not in _NO_SSML_LANGS
    )

    if voice_uses_ssml:
        ssml_text = wrap_ssml(text, lang)
        synthesis_input = texttospeech.SynthesisInput(ssml=ssml_text)
    else:
        # Strip any SSML tags to be safe before sending plain text
        clean_text = re.sub(r'<[^>]+>', '', text).strip()
        synthesis_input = texttospeech.SynthesisInput(text=clean_text)

    # ⚡ Use pre-created Sinhala AudioConfig (Fix 5)
    audio_config = _SINHALA_AUDIO_CONFIG if lang == "si" else AUDIO_CONFIG

    logger.info("TTS → lang=%s  voice=%s  ssml=%s  chars=%d",
                lang, voice_name or "(auto)", voice_uses_ssml, len(text))

    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )

    logger.info("TTS ← audio_bytes=%d", len(response.audio_content))

    # ⚡ Cache short, likely-repeated phrases (Fix 5)
    if len(text) <= _CACHE_MAX_TEXT_LEN:
        _audio_cache[cache_key] = response.audio_content

    return response.audio_content


def synthesize_multilang(segments: list[tuple[str, str]]) -> bytes:
    """
    Synthesize multiple (text, lang) segments each with their own native
    voice and concatenate the raw MP3 bytes.

    Parameters
    ----------
    segments : list of (text, lang) tuples
        e.g. [("ආයුබෝවන්!", "si"), ("வணக்கம்!", "ta"), ("Hello!", "en")]

    Returns
    -------
    bytes : concatenated MP3 audio (safe to serve directly)
    """
    parts: list[bytes] = []
    for text, lang in segments:
        if not text.strip():
            continue
        try:
            audio = synthesize(text.strip(), lang, use_ssml=False)
            parts.append(audio)
        except Exception as exc:
            logger.warning("synthesize_multilang: segment lang=%s failed: %s", lang, exc)
    return b"".join(parts)


def synthesize_greeting() -> bytes:
    """
    Synthesize the full trilingual greeting with each language segment
    rendered by its own native voice, then concatenated.

    Sinhala  → si-LK auto (Google Cloud native Sinhala)
    Tamil    → ta-IN-Chirp3-HD-Aoede (highest quality neural Tamil)
    English  → en-US-Journey-F (conversational neural)
    """
    from src.language import (
        _GREETING_SI,
        _GREETING_TA,
        _GREETING_EN,
    )
    return synthesize_multilang([
        (_GREETING_SI, "si"),
        (_GREETING_TA, "ta"),
        (_GREETING_EN, "en"),
    ])


def synthesize_mulaw(text: str, lang: str) -> bytes:
    """
    Synthesize text directly to 8kHz MULAW for Twilio streaming.
    Bypasses MP3 conversion to save CPU and reduce latency.

    ⚡ Short phrases are served from _audio_cache if pre-warmed at startup.
    """
    # ⚡ Cache hit — serve from memory (zero TTS latency for pre-warmed phrases)
    cache_key = f"{lang}:{text[:200]}"
    if len(text) <= _CACHE_MAX_TEXT_LEN and cache_key in _audio_cache:
        logger.info("TTS MULAW cache hit → lang=%s  chars=%d", lang, len(text))
        return _audio_cache[cache_key]

    tts_client = _get_tts_client()
    voice = VOICES.get(lang, VOICES["en"])

    # Strip any SSML tags for plain text synthesis (streaming usually simpler)
    clean_text = re.sub(r'<[^>]+>', '', text).strip()
    synthesis_input = texttospeech.SynthesisInput(text=clean_text)

    logger.info("TTS Streaming → lang=%s  chars=%d", lang, len(text))

    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=_MULAW_AUDIO_CONFIG,
    )

    # ⚡ Cache result for future calls (only short phrases)
    if len(text) <= _CACHE_MAX_TEXT_LEN:
        _audio_cache[cache_key] = response.audio_content

    return response.audio_content
