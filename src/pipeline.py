# src/pipeline.py
from __future__ import annotations
"""
End-to-end pipeline: audio bytes → STT → LLM → TTS → audio bytes.
This is the single function the server calls per caller utterance.
"""

import logging

from src.stt import transcribe_from_url
from src.llm import chat
from src.tts import synthesize, wrap_ssml

logger = logging.getLogger(__name__)


def process_recording(
    recording_url: str,
    history: list[dict],
    locked_lang: str | None = None,
    call_sid: str = "",
) -> tuple[bytes, list[dict], str, bool]:
    """
    Full pipeline for a Twilio recording URL.

    Parameters
    ----------
    recording_url : str
        Public URL of the Twilio recording (MP3 or WAV).
    history : list[dict]
        Current conversation history (Gemini messages format).
    locked_lang : str | None
        Language code locked at session start ('si', 'ta', 'en').
        When set, language detection is skipped inside chat().

    Returns
    -------
    audio_bytes : bytes
        MP3 audio of Nimali's reply, ready to stream back via Twilio.
    updated_history : list[dict]
        Conversation history with this turn appended.
    detected_lang : str
        'si', 'ta', or 'en'
    should_escalate : bool
        True if Gemini triggered an escalation.
    """
    # ── 1. Speech → Text ──────────────────────────────────────────────
    transcript, stt_lang, _confidence = transcribe_from_url(recording_url)

    if not transcript:
        logger.warning("Empty transcript received; sending fallback reply.")
        transcript = "..."   # LLM will produce a polite "pardon?" response

    logger.info("Pipeline STT: lang=%s  text=%r", stt_lang, transcript[:80])

    # ── 2. LLM ────────────────────────────────────────────────────────
    reply_text, updated_history, detected_lang, should_escalate, should_hangup = chat(
        transcript, history,
        stt_lang=stt_lang,
        locked_lang=locked_lang,
        call_sid=call_sid,
    )

    logger.info("Pipeline LLM: reply=%r  escalate=%s  hangup=%s", reply_text[:80], should_escalate, should_hangup)

    # ── 3. Text → Speech ──────────────────────────────────────────────────
    # synthesize() handles SSML wrapping internally based on voice support.
    audio_bytes = synthesize(reply_text, detected_lang, use_ssml=True)

    logger.info("Pipeline TTS: %d bytes of MP3", len(audio_bytes))
    return audio_bytes, updated_history, detected_lang, should_escalate


def process_audio_bytes(
    audio_bytes_in: bytes,
    history: list[dict],
    locked_lang: str | None = None,
    call_sid: str = "",
) -> tuple[bytes, list[dict], str, bool]:
    """
    Same pipeline but accepts raw audio bytes instead of a URL.
    Useful for testing without Twilio.
    """
    from src.stt import transcribe

    transcript, stt_lang, _confidence = transcribe(audio_bytes_in)
    if not transcript:
        transcript = "..."

    reply_text, updated_history, detected_lang, should_escalate, should_hangup = chat(
        transcript, history,
        stt_lang=stt_lang,
        locked_lang=locked_lang,
        call_sid=call_sid,
    )

    # synthesize() handles SSML wrapping internally based on voice support.
    audio_bytes_out = synthesize(reply_text, detected_lang, use_ssml=True)

    return audio_bytes_out, updated_history, detected_lang, should_escalate
