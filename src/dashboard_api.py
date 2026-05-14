# src/dashboard_api.py
"""
FastAPI router for the CMC Assistant dashboard.

Mounted at /dashboard/api — provides:
  - Local agent tester (text + voice)
  - Twilio call conversation viewer
  - Agent settings editor
  - Connection health checks
"""

import io
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from src.db import (
    get_call, get_stats, list_calls, list_bookings, list_complaints,
    list_all_slots, update_slot_status
)
from src.llm import chat
from src.tts import synthesize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard/api", tags=["dashboard"])

ROOT = Path(__file__).parent.parent
PROMPT_FILE = ROOT / "trilingual_agent_prompt.md"
ENV_FILE = ROOT / ".env"

# ── Editable LLM params (live-adjustable via settings panel) ──────────────
_llm_params: dict[str, Any] = {
    "max_tokens": 500,       # ⚡ 500 tokens — enough for 3-4 complete Sinhala/Tamil sentences
    "temperature": 0.3,      # ⚡ Low temp = fewer hallucinations + faster sampling
}


# ── Reference to the active sessions dict from server.py ──────────────────
# Injected by server.py after import.
# Each value is: {"history": list[dict], "lang": str|None, "lang_confirmed": bool}
_sessions_ref: dict[str, dict] = {}


def set_sessions_ref(sessions: dict) -> None:
    global _sessions_ref
    _sessions_ref = sessions


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 1 — Local Agent Tester
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/test-greeting")
def test_greeting():
    """
    Return the language-selection greeting text + synthesised audio
    so the tester can display the bubble AND auto-play the voice.

    Each language segment (SI / TA / EN) is rendered by its own native
    voice and the results are concatenated, so callers hear natural-sounding
    speech in their own language rather than an English engine reading
    Sinhala/Tamil text.
    """
    import base64
    from src.language import LANG_SELECTION_GREETING
    from src.tts import synthesize_greeting

    audio_b64 = ""
    try:
        audio_bytes = synthesize_greeting()
        audio_b64   = base64.b64encode(audio_bytes).decode()
    except Exception as exc:
        logger.warning("Greeting TTS failed: %s", exc)

    return {"greeting": LANG_SELECTION_GREETING, "audio_b64": audio_b64}



@router.post("/test-text")
async def test_text(request: Request):
    """
    Test the agent with a typed message. Supports the two-phase flow:

    Phase 1 — language selection:
      Body (button click):  { "lang_phase": 1, "lang_code": "si", ... }
        → lang_code bypasses NLP detection entirely (direct code from button)
      Body (typed text):    { "lang_phase": 1, "message": "sinhala", ... }
        → detect_language_choice() on the typed text

    Phase 2 — normal support conversation:
      Body: { "message": "...", "history": [...],
              "lang_phase": 2, "locked_lang": "si" }
      → chat() with locked_lang, return reply + audio
    """
    body = await request.json()
    message: str    = body.get("message", "").strip()
    history: list   = body.get("history", [])
    lang_phase: int = body.get("lang_phase", 2)   # default: skip to support
    locked_lang: str = body.get("locked_lang", "")
    # Step 2: direct lang code from button (skips NLP detection)
    lang_code: str  = body.get("lang_code", "").strip().lower()

    if not message and not lang_code:
        raise HTTPException(status_code=400, detail="message or lang_code is required")

    # ── Phase 1: detect which language the user chose ─────────────────────
    if lang_phase == 1:
        from src.language import (
            LANG_CONFIRMATIONS,
            LANG_RETRY_PROMPT,
        )

        chosen = None
        # Step 2: if a valid direct code is provided (from button), use it immediately
        if lang_code in ("si", "ta", "en"):
            chosen = lang_code
        else:
            # Map digits from typed text
            if message in ("1", "2", "3"):
                chosen_map = {"1": "si", "2": "ta", "3": "en"}
                chosen = chosen_map[message]
            else:
                from src.language import detect_language_choice
                chosen = detect_language_choice(message)

        if chosen:
            confirmation = LANG_CONFIRMATIONS[chosen]
            try:
                audio_bytes = synthesize(confirmation, chosen)
                audio_b64 = __import__("base64").b64encode(audio_bytes).decode()
            except Exception:
                audio_b64 = ""
            return {
                "phase": 1,
                "chosen_lang": chosen,
                "reply": confirmation,
                "lang": chosen,
                "escalate": False,
                "history": history,
                "audio_b64": audio_b64,
            }
        else:
            # Unclear — retry
            retry_text = LANG_RETRY_PROMPT["en"]
            try:
                audio_bytes = synthesize(retry_text, "en")
                audio_b64 = __import__("base64").b64encode(audio_bytes).decode()
            except Exception:
                audio_b64 = ""
            return {
                "phase": 1,
                "chosen_lang": None,
                "reply": retry_text,
                "lang": "en",
                "escalate": False,
                "history": history,
                "audio_b64": audio_b64,
            }

    # ── Phase 2: normal support conversation (language locked) ────────────
    try:
        reply_text, updated_history, lang, should_escalate, should_hangup = chat(
            message, history,
            locked_lang=locked_lang if locked_lang in ("si", "ta", "en") else None,
        )
        audio_bytes = synthesize(reply_text, lang)
        audio_b64 = __import__("base64").b64encode(audio_bytes).decode()

        return {
            "phase": 2,
            "reply": reply_text,
            "lang": lang,
            "escalate": should_escalate,
            "history": updated_history,
            "audio_b64": audio_b64,
        }
    except Exception as exc:
        logger.error("test-text error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))



@router.post("/test-still-there")
async def test_still_there(request: Request):
    """
    Synthesize and return the "are you still there?" prompt for the given language.

    Body: { "lang": "si" | "ta" | "en", "last_question": "<optional last agent text>" }
    Returns: { "text": "...", "audio_b64": "..." }

    Used by the dashboard tester to play real TTS audio instead of just showing
    the text bubble — matching the behaviour of a live Twilio call.
    """
    import base64
    from src.language import STILL_THERE_PROMPTS

    body = await request.json()
    lang: str = body.get("lang", "en")
    if lang not in ("si", "ta", "en"):
        lang = "en"

    text = STILL_THERE_PROMPTS[lang]
    audio_b64 = ""
    try:
        audio_bytes = synthesize(text, lang)
        audio_b64 = base64.b64encode(audio_bytes).decode()
    except Exception as exc:
        logger.warning("test-still-there TTS failed: %s", exc)

    return {"text": text, "audio_b64": audio_b64}


@router.post("/test-end-call")
async def test_end_call(request: Request):
    """
    Synthesize and return the goodbye phrase for the given language.

    Body: { "lang": "si" | "ta" | "en" }
    Returns: { "text": "...", "audio_b64": "..." }

    Called by the dashboard tester when the second silence strike fires,
    so the goodbye is spoken in TTS before the session resets.
    """
    import base64
    from src.language import GOODBYE_PROMPTS

    body = await request.json()
    lang: str = body.get("lang", "en")
    if lang not in ("si", "ta", "en"):
        lang = "en"

    text = GOODBYE_PROMPTS[lang]
    audio_b64 = ""
    try:
        audio_bytes = synthesize(text, lang)
        audio_b64 = base64.b64encode(audio_bytes).decode()
    except Exception as exc:
        logger.warning("test-end-call TTS failed: %s", exc)

    return {"text": text, "audio_b64": audio_b64}


@router.post("/test-voice")
async def test_voice(
    audio: UploadFile = File(...), 
    history: str = Form(default="[]"),
    lang_phase: int = Form(default=2),
    locked_lang: str = Form(default=""),
    stt_only: bool = Form(default=False)
):
    """
    Test the agent with a recorded voice clip.
    Accepts webm/wav/ogg audio from the browser MediaRecorder.
    """
    from src.stt import transcribe
    from src.pipeline import process_audio_bytes

    try:
        history_list = json.loads(history)
    except Exception:
        history_list = []

    raw_bytes = await audio.read()

    # If it's a webm file (like from MediaRecorder), and ffmpeg is missing, 
    # we can try to send it directly to Google STT as WEBM_OPUS.
    from google.cloud import speech
    pcm_bytes = _convert_to_pcm(raw_bytes)

    try:
        # STT
        if pcm_bytes == raw_bytes: 
            # ffmpeg failed/missing, meaning it's still raw webm
            transcript, stt_lang = transcribe(
                pcm_bytes, 
                sample_rate_hertz=None, 
                encoding=speech.RecognitionConfig.AudioEncoding.WEBM_OPUS
            )
        else:
            # Successfully converted to 16kHz PCM
            transcript, stt_lang, _confidence = transcribe(pcm_bytes, sample_rate_hertz=16000)
            
        if not transcript:
            return {"transcript": "", "reply": "", "lang": "si", "escalate": False,
                    "history": history_list, "audio_b64": "", "error": "Could not transcribe audio"}

        if lang_phase == 1 or stt_only:
            # Just return the transcript. The frontend will handle it (lang detect or yes-intercept).
            return {
                "transcript": transcript,
                "reply": "",
                "lang": stt_lang,
                "escalate": False,
                "history": history_list,
                "audio_b64": ""
            }

        # LLM
        reply_text, updated_history, lang, should_escalate, should_hangup = chat(
            transcript, history_list,
            locked_lang=locked_lang or stt_lang,
            call_sid="dashboard_test"
        )

        # TTS
        audio_bytes = synthesize(reply_text, lang)
        audio_b64 = __import__("base64").b64encode(audio_bytes).decode()

        return {
            "transcript": transcript,
            "reply": reply_text,
            "lang": lang,
            "escalate": should_escalate,
            "history": updated_history,
            "audio_b64": audio_b64,
        }
    except Exception as exc:
        logger.error("test-voice error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


def _convert_to_pcm(raw: bytes) -> bytes:
    """Try ffmpeg conversion; fall back to raw bytes."""
    try:
        import subprocess
        import shutil
        if not shutil.which("ffmpeg"):
            return raw
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as fin:
            fin.write(raw)
            fin_name = fin.name
        fout_name = fin_name.replace(".webm", ".raw")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", fin_name,
             "-f", "s16le", "-ar", "16000", "-ac", "1", fout_name],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0:
            data = open(fout_name, "rb").read()
            os.unlink(fin_name)
            os.unlink(fout_name)
            return data
    except Exception as e:
        logger.warning("ffmpeg conversion failed: %s", e)
    return raw


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 2 — Twilio Conversations
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/calls")
def api_list_calls(limit: int = 50, offset: int = 0, status: str = "all"):
    """List call records from the database."""
    calls = list_calls(limit=limit, offset=offset, status_filter=status)
    return {"calls": calls, "count": len(calls)}


@router.get("/bookings")
def api_list_bookings(limit: int = 50, offset: int = 0, category: str = "all"):
    """List bookings from the database."""
    bookings = list_bookings(limit=limit, offset=offset, category_filter=category)
    return {"bookings": bookings, "count": len(bookings)}


@router.get("/complaints")
def api_list_complaints(limit: int = 50, offset: int = 0, category: str = "all"):
    """List complaints from the database."""
    complaints = list_complaints(limit=limit, offset=offset, category_filter=category)
    return {"complaints": complaints, "count": len(complaints)}


@router.get("/slots")
def api_list_slots():
    """List all available slots."""
    return {"slots": list_all_slots()}


@router.put("/slots/{slot_id}")
async def api_toggle_slot(slot_id: int, request: Request):
    """Toggle slot active status."""
    data = await request.json()
    is_active = data.get("is_active", True)
    update_slot_status(slot_id, is_active)
    return {"success": True}


@router.get("/calls/active")
def api_active_calls():
    """Return currently in-progress calls from the in-memory sessions store."""
    active = []
    for sid, session in _sessions_ref.items():
        # Handle both old list format and new dict format gracefully
        if isinstance(session, dict):
            hist = session.get("history", [])
            lang = session.get("lang")
            lang_confirmed = session.get("lang_confirmed", False)
        else:
            hist = session
            lang = None
            lang_confirmed = False
        active.append({
            "call_sid": sid,
            "turns": len(hist),
            "history": hist,
            "lang": lang,
            "lang_confirmed": lang_confirmed,
        })
    return {"active": active, "count": len(active)}


@router.get("/calls/{call_sid}")
def api_get_call(call_sid: str):
    """Return full transcript for a single call."""
    call = get_call(call_sid)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/stats")
def api_stats():
    """Aggregated statistics."""
    return get_stats()


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 3 — Agent Settings
# ═══════════════════════════════════════════════════════════════════════════

# Allowed keys to surface in the UI (never expose API key values)
_VISIBLE_KEYS = [
    "BASE_URL",
    "ESCALATION_NUMBER",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_ACCOUNT_SID",
]
_SENSITIVE_KEYS = [
    "GEMINI_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
]


def _parse_env() -> dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict[str, str]) -> None:
    current = _parse_env()
    current.update(updates)
    lines = []
    if ENV_FILE.exists():
        raw_lines = ENV_FILE.read_text().splitlines()
        replaced = set()
        for line in raw_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    lines.append(f"{k}={updates[k]}")
                    replaced.add(k)
                    continue
            lines.append(line)
        for k, v in updates.items():
            if k not in replaced:
                lines.append(f"{k}={v}")
    else:
        for k, v in updates.items():
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


@router.get("/settings")
def api_get_settings():
    env = _parse_env()
    visible = {k: env.get(k, "") for k in _VISIBLE_KEYS}
    sensitive = {k: ("*" * 12 if env.get(k) else "") for k in _SENSITIVE_KEYS}
    from src.tts import AUDIO_CONFIG
    return {
        "env": {**visible, **sensitive},
        "llm_params": _llm_params,
        "tts_params": {
            "speaking_speed": getattr(AUDIO_CONFIG, "speaking_rate", 1.0),
            "pitch": getattr(AUDIO_CONFIG, "pitch", 0.0),
            "volume_gain_db": getattr(AUDIO_CONFIG, "volume_gain_db", 0.0),
        },
        "sensitive_keys": _SENSITIVE_KEYS,
    }


@router.put("/settings")
async def api_put_settings(request: Request):
    body = await request.json()
    env_updates: dict = body.get("env", {})
    llm_updates: dict = body.get("llm_params", {})
    tts_updates: dict = body.get("tts_params", {})

    # Only allow writing non-sensitive or explicitly provided sensitive values
    safe_env = {k: v for k, v in env_updates.items()
                if k in _VISIBLE_KEYS or (k in _SENSITIVE_KEYS and v and not v.startswith("*"))}
    if safe_env:
        _write_env(safe_env)
        for k, v in safe_env.items():
            os.environ[k] = v

    if llm_updates:
        if "max_tokens" in llm_updates:
            _llm_params["max_tokens"] = int(llm_updates["max_tokens"])
        if "temperature" in llm_updates:
            _llm_params["temperature"] = float(llm_updates["temperature"])

    if tts_updates:
        from src.tts import AUDIO_CONFIG
        if "speaking_speed" in tts_updates:
            AUDIO_CONFIG.speaking_rate = float(tts_updates["speaking_speed"])
        if "pitch" in tts_updates:
            AUDIO_CONFIG.pitch = float(tts_updates["pitch"])
        if "volume_gain_db" in tts_updates:
            AUDIO_CONFIG.volume_gain_db = float(tts_updates["volume_gain_db"])

    from src.tts import AUDIO_CONFIG
    return {
        "ok": True, 
        "saved_env_keys": list(safe_env.keys()), 
        "llm_params": _llm_params, 
        "tts_params": {
            "speaking_speed": getattr(AUDIO_CONFIG, "speaking_rate", 1.0),
            "pitch": getattr(AUDIO_CONFIG, "pitch", 0.0),
            "volume_gain_db": getattr(AUDIO_CONFIG, "volume_gain_db", 0.0),
        }
    }


@router.get("/prompt")
def api_get_prompt():
    if not PROMPT_FILE.exists():
        return {"prompt": "", "path": str(PROMPT_FILE)}
    return {"prompt": PROMPT_FILE.read_text(), "path": str(PROMPT_FILE)}


@router.put("/prompt")
async def api_put_prompt(request: Request):
    body = await request.json()
    prompt_text = body.get("prompt", "")
    PROMPT_FILE.write_text(prompt_text)
    return {"ok": True, "length": len(prompt_text)}


@router.post("/test-connections")
async def api_test_connections():
    results = {}

    # Gemini
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
        client.models.generate_content(
            model="gemini-2.5-flash",
            contents="ping",
            config={"max_output_tokens": 5},
        )
        results["gemini"] = {"ok": True, "latency_ms": 0}
    except Exception as e:
        results["gemini"] = {"ok": False, "error": str(e)}

    # Google Cloud
    try:
        from google.cloud import texttospeech
        tts = texttospeech.TextToSpeechClient()
        results["google"] = {"ok": True}
    except Exception as e:
        results["google"] = {"ok": False, "error": str(e)}

    # Twilio
    try:
        from twilio.rest import Client as TwilioClient
        tc = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID", ""),
            os.getenv("TWILIO_AUTH_TOKEN", ""),
        )
        account = tc.api.accounts(os.getenv("TWILIO_ACCOUNT_SID", "")).fetch()
        results["twilio"] = {"ok": True, "account_name": account.friendly_name}
    except Exception as e:
        results["twilio"] = {"ok": False, "error": str(e)}

    # Database
    try:
        from src.db import check_db_connection
        if check_db_connection():
            results["db"] = {"ok": True, "account_name": "Connected"}
        else:
            results["db"] = {"ok": False, "error": "Connection failed"}
    except Exception as e:
        results["db"] = {"ok": False, "error": str(e)}

    return results
