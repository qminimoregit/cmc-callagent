# src/server.py
from __future__ import annotations
"""
FastAPI server — Twilio Voice webhook handler for Nimali.

Call flow
─────────
  1. Twilio dials in          → POST /voice
  2. Server plays greeting    → TwiML <Play> + <Record>
  3. Caller speaks            → Twilio records
  4. Twilio posts recording   → POST /gather
  5. Server: STT → LLM → TTS → streams reply audio URL back as TwiML <Play>
  6. Loop back to <Record>    → or <Dial> if [ESCALATE]
"""

import asyncio
import base64
from contextlib import asynccontextmanager
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import json
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Gather

from src.pipeline import process_recording
from src import db as call_db
from src.dashboard_api import router as dashboard_router, set_sessions_ref
from src.session_store import get_session, save_session, clear_session, set_call_start, pop_call_start
from src.language import (
    LANG_SELECTION_GREETING,
    LANG_CONFIRMATIONS,
    LANG_RETRY_PROMPT,
    STILL_THERE_PROMPTS,
    GOODBYE_PROMPTS,
    detect_yes,
)
from src.tts import synthesize
from src.stt import transcribe_from_url
from src.live_handler import media_stream_live

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
ESCALATION_NUMBER = os.getenv("ESCALATION_NUMBER", "")   # E.164 format e.g. +94112345678
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # ALB DNS / public HTTPS URL (trailing slash removed)
STATIC_DIR = Path(__file__).parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

# ── S3 audio storage (production) ───────────────────────────────────────────
S3_AUDIO_BUCKET = os.getenv("S3_AUDIO_BUCKET", "")
_s3 = boto3.client("s3", region_name="ap-south-1") if S3_AUDIO_BUCKET else None

# ── Lifespan event handler (replaces deprecated @app.on_event) ─────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ARG001
    await _startup()
    yield  # server is running
    # nothing to clean up on shutdown yet

async def _startup() -> None:
    call_db.init_db()
    set_sessions_ref({})

    # 🧹 Cleanup stale calls from previous runs
    try:
        cleaned = call_db.cleanup_stale_calls(timeout_minutes=30)
        if cleaned > 0:
            logger.info("🧹 Cleaned up %d stale 'in-progress' calls on startup.", cleaned)
    except Exception as e:
        logger.warning("Stale call cleanup failed: %s", e)

    # ⚡ Pre-warm API clients — eliminates cold-start latency on first call
    try:
        from src.stt import _get_speech_client, _get_http_client
        from src.tts import _get_tts_client
        from src.llm import _get_client
        _get_speech_client()
        _get_http_client()
        _get_tts_client()
        _get_client()
        logger.info("All API clients pre-warmed at startup ⚡")
    except Exception as e:
        logger.warning("Client pre-warm failed (will lazy-init on first request): %s", e)

    logger.info("Dashboard available at /dashboard")

    # ── Pre-generate the trilingual greeting MP3 if not already present ──────
    greeting_path = STATIC_DIR / "lang_select_greeting_v2.mp3"
    if not greeting_path.exists():
        try:
            from src.tts import synthesize_greeting
            logger.info("Generating trilingual greeting MP3...")
            audio = synthesize_greeting()
            greeting_path.write_bytes(audio)
            logger.info("Greeting saved to %s (%d bytes)", greeting_path, len(audio))
        except Exception as e:
            logger.warning("Could not pre-generate greeting: %s", e)

    # ⚡ Fix 8: Pre-synthesize all common short phrases into the TTS MULAW cache.
    # These are served from memory on every call — zero TTS latency for these turns.
    try:
        from src.tts import synthesize_mulaw, _audio_cache
        from src.language import (
            STILL_THERE_PROMPTS, GOODBYE_PROMPTS, REPEAT_PROMPTS, LANG_CONFIRMATIONS,
        )
        _prewarm_phrases: list[tuple[str, str]] = [
            *[(text, lang) for lang, text in STILL_THERE_PROMPTS.items()],
            *[(text, lang) for lang, text in GOODBYE_PROMPTS.items()],
            *[(text, lang) for lang, text in REPEAT_PROMPTS.items()],
            *[(text, lang) for lang, text in LANG_CONFIRMATIONS.items()],
        ]
        warmed = 0
        for text, lang in _prewarm_phrases:
            cache_key = f"{lang}:{text[:200]}"
            if cache_key not in _audio_cache:
                try:
                    _audio_cache[cache_key] = synthesize_mulaw(text, lang)
                    warmed += 1
                except Exception as _e:
                    logger.debug("Phrase pre-warm skipped (%s): %s", lang, _e)
        logger.info("⚡ Pre-warmed %d common TTS phrases into cache", warmed)
    except Exception as e:
        logger.warning("TTS phrase pre-warm failed: %s", e)

    # ⚡ Pre-warmed common TTS phrases into cache
    try:
        warmed = 0
        from src.tts import synthesize
        # Add common phrases to pre-warm if needed
        logger.info("⚡ Pre-warming common TTS phrases into cache...")
        # ... existing pre-warm logic ...
    except Exception as e:
        logger.warning("TTS phrase pre-warm failed: %s", e)

# ── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="Nimali — Trilingual Call Agent",
    description="Sinhala / Tamil / English customer support agent powered by Gemini 2.5 Flash + Google STT/TTS",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files so Twilio can fetch generated MP3s
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Include dashboard API router
app.include_router(dashboard_router)

# Mount dashboard frontend
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
DASHBOARD_DIR.mkdir(exist_ok=True)
app.mount("/dashboard", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

# Add the live stream handler
app.add_api_websocket_route("/media-stream-live", media_stream_live)

# _call_start is now stored in Redis via set_call_start / pop_call_start
# so it is shared safely across all Gunicorn worker processes.


# ── Helper: save MP3 bytes — S3 in production, local static in dev ──────────
def _save_audio(audio_bytes: bytes, call_sid: str) -> str:
    filename = f"{call_sid}_{uuid.uuid4().hex[:8]}.mp3"
    if _s3 and S3_AUDIO_BUCKET:
        _s3.put_object(
            Bucket=S3_AUDIO_BUCKET,
            Key=f"audio/{filename}",
            Body=audio_bytes,
            ContentType="audio/mpeg",
        )
        return f"https://{S3_AUDIO_BUCKET}.s3.ap-south-1.amazonaws.com/audio/{filename}"
    # Fallback: local /static (dev mode)
    filepath = STATIC_DIR / filename
    filepath.write_bytes(audio_bytes)
    return f"{BASE_URL}/static/{filename}"


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Liveness probe."""
    from src.db import check_db_connection
    db_status = "connected" if check_db_connection() else "disconnected"
    return {"status": "ok", "agent": "Nimali", "db_status": db_status}


@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html><head><title>Nimali — Trilingual Call Agent</title></head>
    <body style="font-family:sans-serif;max-width:600px;margin:40px auto;">
      <h1>🇱🇰 Nimali — Trilingual Call Agent</h1>
      <p>Languages: <strong>Sinhala · Tamil · English</strong></p>
      <p>Webhook routes:</p>
      <ul>
        <li><code>POST /voice</code> — Twilio incoming call webhook</li>
        <li><code>POST /gather</code> — Twilio recording callback</li>
        <li><code>GET  /health</code> — Liveness probe</li>
      </ul>
    </body></html>
    """


@app.post("/voice")
async def voice(request: Request):
    """
    Twilio calls this when a new call arrives.
    Plays the trilingual language-selection greeting, then records the
    caller's language choice.
    """
    form = await request.form()
    call_sid: str = str(form.get("CallSid", "unknown"))
    phone_number: str = str(form.get("From", ""))
    caller_city: str = str(form.get("CallerCity", ""))
    caller_country: str = str(form.get("CallerCountry", ""))
    logger.info("Incoming call: CallSid=%s From=%s", call_sid, phone_number)

    # Initialise session
    get_session(call_sid)
    set_call_start(call_sid, time.time())

    # Persist to DB
    call_db.upsert_call(
        call_sid=call_sid,
        phone_number=phone_number,
        caller_city=caller_city,
        caller_country=caller_country,
        status="in-progress",
    )

    # Note: statusCallback is configured on the Twilio number/call, not in TwiML.
    response = VoiceResponse()

    gather_node = Gather(
        input="dtmf speech",
        num_digits=1,
        timeout=5,
        action=f"{BASE_URL}/gather",
        speech_timeout="auto"
    )

    # Play the language-selection greeting (pre-generated MP3 preferred)
    lang_select_path = STATIC_DIR / "lang_select_greeting_v2.mp3"
    if lang_select_path.exists() and BASE_URL:
        gather_node.play(f"{BASE_URL}/static/lang_select_greeting_v2.mp3")
    else:
        # Fallback: Twilio built-in TTS (English only)
        gather_node.say(
            "Welcome to the Colombo Municipal Council service centre. "
            "You may continue in Sinhala, Tamil, or English. "
            "For Sinhala, press 1. For Tamil, press 2. For English, press 3.",
            voice="Polly.Salli",
            language="en-US",
        )

    response.append(gather_node)

    return Response(content=str(response), media_type="application/xml")


@app.post("/gather")
async def gather(request: Request):
    """
    Twilio posts here after recording the caller's utterance.

    Phase 1 (lang not yet confirmed): detect language choice, play
    confirmation, then prompt for the caller's actual question.

    Phase 2 (lang confirmed): run STT → LLM → TTS in the locked
    language and loop back.
    """
    form = await request.form()
    call_sid: str = str(form.get("CallSid", "unknown"))
    digits: str = str(form.get("Digits", ""))
    recording_url: str = str(form.get("RecordingUrl", ""))
    recording_duration: int = int(str(form.get("RecordingDuration", 0) or 0))
    call_status: str = str(form.get("CallStatus", ""))

    logger.info("Gather: CallSid=%s  status=%s  digits=%s  url=%s  duration=%s", call_sid, call_status, digits, recording_url, recording_duration)

    session = get_session(call_sid)
    response = VoiceResponse()

    # ── Silence / timeout check — caller didn't speak or press a key ─────────────────────────
    if not digits and (not recording_url or recording_duration == 0):
        locked = session.get("lang") or "en"
        strikes = session.get("silence_strikes", 0)

        if strikes == 0:
            # ── Strike 1: play "are you still there?" and give another chance ──
            logger.info("Silence strike 1 for call %s — playing inactivity prompt", call_sid)
            prompt_text = STILL_THERE_PROMPTS.get(locked, STILL_THERE_PROMPTS["en"])
            silence_audio = await asyncio.to_thread(synthesize, prompt_text, locked)
            
            if not session.get("lang_confirmed"):
                gather_node = Gather(
                    input="dtmf speech",
                    num_digits=1,
                    timeout=5,
                    action=f"{BASE_URL}/gather",
                    speech_timeout="auto"
                )
                gather_node.play(_save_audio(silence_audio, call_sid))
                response.append(gather_node)
            else:
                response.play(_save_audio(silence_audio, call_sid))
                response.record(
                    action=f"{BASE_URL}/gather",
                    method="POST",
                    max_length=12,
                    timeout=20,   # shorter second-chance window
                    transcribe=False,
                    play_beep=False,
                )
            session["silence_strikes"] = 1
            save_session(call_sid, session)
        else:
            # ── Strike 2: play goodbye and hang up ──────────────────────────
            logger.info("Silence strike 2 for call %s — ending call", call_sid)
            goodbye_text = GOODBYE_PROMPTS.get(locked, GOODBYE_PROMPTS["en"])
            goodbye_audio = await asyncio.to_thread(synthesize, goodbye_text, locked)
            response.play(_save_audio(goodbye_audio, call_sid))
            response.hangup()
            call_db.upsert_call(call_sid=call_sid, status="completed")
            clear_session(call_sid)

        return Response(content=str(response), media_type="application/xml")

    # Request WAV from Twilio — better quality than MP3 for Sinhala/Tamil STT
    if recording_url and not recording_url.endswith((".mp3", ".wav")):
        recording_url += ".wav"

    # ── Phase 1: language selection ───────────────────────────────────────────
    if not session["lang_confirmed"]:
        try:
            chosen = None
            if digits in ["1", "2", "3"]:
                chosen_map = {"1": "si", "2": "ta", "3": "en"}
                chosen = chosen_map[digits]
                logger.info("Lang-select choice: %s", chosen)

            if chosen:
                session["lang"] = chosen
                session["lang_confirmed"] = True
                save_session(call_sid, session)

                # ── Phase 1: Confirmation + Stream transition ─────────────
                # Play the confirmation audio FIRST via TwiML <Play>, then
                # open the WebSocket stream. This is the only correct way:
                # - <Stream> only carries audio FROM Twilio TO the server (inbound)
                # - Sending audio back through the WebSocket echoes into VAD
                #   and causes a spurious utterance that kills the conversation.

                from src.language import LANG_CONFIRMATIONS
                confirm_text = LANG_CONFIRMATIONS.get(chosen, "Hello, how can I help you?")
                try:
                    confirm_audio = await asyncio.to_thread(synthesize, confirm_text, chosen)
                    response.play(_save_audio(confirm_audio, call_sid))
                    logger.info("Confirmation audio queued for call %s (lang=%s)", call_sid, chosen)
                except Exception as exc:
                    logger.warning("Could not synthesize confirmation for call %s: %s", call_sid, exc)

                # Construct WebSocket URL from BASE_URL (CloudPanel handles the SSL certificate)
                ws_scheme = "wss://" if BASE_URL.startswith("https") else "ws://"
                wss_url = BASE_URL.replace("https://", ws_scheme).replace("http://", ws_scheme)
                
                logger.info("Connecting WebSocket Media Stream for call %s → %s", call_sid, wss_url)
                response.pause(length=1)
                response.connect().stream(url=f"{wss_url}/media-stream-live")  # type: ignore[attr-defined]
            else:
                # Could not detect language — play retry prompt and re-record
                logger.warning("Language choice unclear; retrying.")
                retry_audio = await asyncio.to_thread(synthesize, LANG_RETRY_PROMPT["en"], "en")
                gather_node = Gather(
                    input="dtmf speech",
                    num_digits=1,
                    timeout=5,
                    action=f"{BASE_URL}/gather",
                    speech_timeout="auto"
                )
                gather_node.play(_save_audio(retry_audio, call_sid))
                response.append(gather_node)

        except Exception as exc:
            logger.error("Lang-select error for call %s: %s", call_sid, exc, exc_info=True)
            gather_node = Gather(
                input="dtmf speech",
                num_digits=1,
                timeout=5,
                action=f"{BASE_URL}/gather",
                speech_timeout="auto"
            )
            gather_node.say(
                "Sorry, something went wrong. Please press 1 for Sinhala, 2 for Tamil, or 3 for English.",
                language="en-US",
            )
            response.append(gather_node)

        return Response(content=str(response), media_type="application/xml")

    # ── Phase 2: normal support loop (language already locked) ───────────────
    locked_lang = session["lang"]
    history = session["history"]

    # ── Transcribe ONCE — reuse for yes-detection AND the LLM pipeline ────────
    try:
        transcript_check, stt_lang, stt_confidence = await asyncio.to_thread(transcribe_from_url, recording_url)
    except Exception as e:
        logger.warning("STT failed for call %s: %s", call_sid, e)
        transcript_check, stt_lang, stt_confidence = "", locked_lang, 0.0

    # ⚡ Fix 3: Low-confidence STT — ask caller to repeat instead of sending garbage to LLM
    if stt_confidence < 0.5 and transcript_check:
        logger.info("Low STT confidence (%.2f) for call %s — asking caller to repeat", stt_confidence, call_sid)
        from src.language import REPEAT_PROMPTS
        repeat_text = REPEAT_PROMPTS.get(locked_lang, REPEAT_PROMPTS["en"])
        repeat_audio = await asyncio.to_thread(synthesize, repeat_text, locked_lang)
        response.play(_save_audio(repeat_audio, call_sid))
        response.record(
            action=f"{BASE_URL}/gather",
            method="POST",
            max_length=10,
            timeout=2,
            transcribe=False,
            play_beep=False,
        )
        session["silence_strikes"] = 0
        save_session(call_sid, session)
        return Response(content=str(response), media_type="application/xml")

    if transcript_check and session.get("silence_strikes", 0) > 0 and detect_yes(transcript_check, locked_lang) and isinstance(session.get("last_agent_question"), str) and session["last_agent_question"]:
        logger.info("Yes-response detected for call %s — replaying last question", call_sid)
        replay_audio = await asyncio.to_thread(synthesize, session["last_agent_question"], locked_lang)
        response.play(_save_audio(replay_audio, call_sid))
        response.record(
            action=f"{BASE_URL}/gather",
            method="POST",
            max_length=10,       # ⚡ Fix 6: reduced from 15 → 10
            timeout=2,
            transcribe=False,
            play_beep=False,
        )
        session["silence_strikes"] = 0   # reset strikes — caller is active
        save_session(call_sid, session)
        return Response(content=str(response), media_type="application/xml")

    # Real speech — reset silence strikes
    session["silence_strikes"] = 0
    save_session(call_sid, session)

    # Use the already-transcribed text — avoids re-downloading inside process_recording
    if not transcript_check:
        transcript_check = "..."   # LLM will produce a polite "pardon?" response

    try:
        from src.llm import chat
        from src.tts import synthesize as tts_synthesize

        reply_text, updated_history, detected_lang, should_escalate, should_hangup = await asyncio.to_thread(
            chat, transcript_check, history,
            stt_lang, locked_lang, call_sid,
        )
        audio_bytes = await asyncio.to_thread(tts_synthesize, reply_text, detected_lang, True)
        session["history"] = updated_history

        # Build turns list from updated history for DB persistence
        turns = []
        for i in range(0, len(updated_history) - 1, 2):
            user_turn = updated_history[i] if i < len(updated_history) else None
            agent_turn = updated_history[i + 1] if i + 1 < len(updated_history) else None
            if user_turn:
                turns.append({"role": "user", "text": user_turn.get("content", ""),
                               "lang": detected_lang,
                               "ts": datetime.now(timezone.utc).isoformat()})
            if agent_turn:
                turns.append({"role": "assistant", "text": agent_turn.get("content", ""),
                               "lang": detected_lang,
                               "ts": datetime.now(timezone.utc).isoformat()})

        if should_escalate and ESCALATION_NUMBER:
            logger.info("Escalating call %s to %s", call_sid, ESCALATION_NUMBER)
            reply_url = _save_audio(audio_bytes, call_sid)
            response.play(reply_url)
            response.dial(ESCALATION_NUMBER)
            call_db.upsert_call(call_sid=call_sid, status="escalated",
                                escalated=True, turns=turns)
            clear_session(call_sid)
        elif should_hangup:
            logger.info("Ending call %s as requested by agent", call_sid)
            reply_url = _save_audio(audio_bytes, call_sid)
            response.play(reply_url)
            response.hangup()
            call_db.upsert_call(call_sid=call_sid, status="completed",
                                escalated=False, turns=turns)
            clear_session(call_sid)
        else:
            reply_url = _save_audio(audio_bytes, call_sid)
            response.play(reply_url)
            response.pause(length=1)  # ⏸ buffer — prevents recording opening mid-reply
            response.record(
                action=f"{BASE_URL}/gather",
                method="POST",
                max_length=15,
                timeout=5,           # 5s silence timeout — avoids cutting off callers mid-thought
                transcribe=False,
                play_beep=False,
            )
            if updated_history:
                last_msg = updated_history[-1]
                if isinstance(last_msg, dict):
                    content = last_msg.get("content", "")
                    # Gemini returns content as a list of part dicts or a plain string
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and "text" in b
                        )
                    session["last_agent_question"] = content or ""
            save_session(call_sid, session)
            call_db.upsert_call(call_sid=call_sid, status="in-progress",
                                escalated=False, turns=turns)

    except Exception as exc:
        logger.error("Pipeline error for call %s: %s", call_sid, exc, exc_info=True)
        # ── Don't hang up on error — play an apology and re-record ──────────
        locked = session.get("lang", "en")
        try:
            from src.language import REPEAT_PROMPTS
            sorry_text = REPEAT_PROMPTS.get(locked, "I'm sorry, I didn't catch that. Could you please repeat?")
            sorry_audio = await asyncio.to_thread(synthesize, sorry_text, locked)
            response.play(_save_audio(sorry_audio, call_sid))
        except Exception:
            response.say("I'm sorry, something went wrong. Please try again.", language="en-US")
        response.record(
            action=f"{BASE_URL}/gather",
            method="POST",
            max_length=15,
            timeout=5,
            transcribe=False,
            play_beep=False,
        )
        call_db.upsert_call(call_sid=call_sid, status="in-progress")

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Handle real-time audio streaming from Twilio via WebSocket.

    Uses VADProcessor to detect utterance boundaries in real-time.
    As soon as the caller stops speaking (~300 ms of silence), the
    captured PCM is sent to STT → LLM → TTS immediately — no fixed
    Twilio timeout wait.

    Pipeline per utterance
    ──────────────────────
    1. VAD detects end of speech  (~300 ms trailing silence)
    2. Google STT on LINEAR16 PCM  (~400–600 ms)
    3. LLM streaming (sentence-by-sentence)  (~800 ms to first sentence)
    4. TTS per sentence + stream back via WebSocket
    """
    await websocket.accept()
    logger.info("WebSocket connection established")

    stream_sid: str | None = None
    call_sid:   str | None = None
    locked_lang = "si"
    history: list[dict] = []

    from src.vad import VADProcessor
    from src.stt import transcribe_pcm, RealtimeTranscriber
    from src.llm import async_stream_chat_with_tools
    from src.tts import synthesize_mulaw
    import audioop  # for MULAW→PCM in media handler

    vad = VADProcessor(
        aggressiveness=2,
        silence_trigger_ms=200,   # ⚡ 200 ms (was 300) — saves 100 ms every turn
        min_speech_ms=80,         # ignore bursts < 80 ms (clicks / breathing)
        pre_speech_pad_ms=80,     # keep 80 ms of pre-speech so first phoneme isn't clipped
    )

    # Semaphore: one utterance processed at a time (prevents overlapping LLM calls)
    _processing_lock = asyncio.Lock()
    # RealtimeTranscriber instance — created when speech onset detected, used when VAD fires
    _realtime_stt: RealtimeTranscriber | None = None
    
    # ⚡ Real-time Queues & Tasks
    audio_queue = asyncio.Queue()
    _current_utterance_task: asyncio.Task | None = None
    
    async def audio_sender_task():
        """Reads TTS payload tasks from the queue and sends them to Twilio in order."""
        while True:
            try:
                item = await audio_queue.get()
                if item is None:
                    audio_queue.task_done()
                    break
                
                if isinstance(item, asyncio.Task):
                    # It's a TTS generation task, wait for it to finish
                    try:
                        payloads = await item
                        for payload in payloads:
                            await websocket.send_json(payload)
                    except asyncio.CancelledError:
                        logger.info("TTS task was cancelled due to barge-in.")
                    except Exception as e:
                        logger.error("Error in TTS task: %s", e)
                else:
                    # Direct message (e.g. clear event)
                    await websocket.send_json(item)
                
                audio_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Audio sender task error: %s", e)
                break
                
    sender_task = asyncio.create_task(audio_sender_task())

    async def generate_tts_payloads(text: str, lang: str, mark_id: int, stream_sid: str) -> list[dict]:
        """Generates TTS audio and returns the WebSocket payloads."""
        audio_payload = await asyncio.to_thread(synthesize_mulaw, text, lang)
        audio_b64 = base64.b64encode(audio_payload).decode("utf-8")
        return [
            {"event": "media", "streamSid": stream_sid, "media": {"payload": audio_b64}},
            {"event": "mark", "streamSid": stream_sid, "mark": {"name": f"reply_done_{mark_id}"}}
        ]

    async def handle_utterance(pcm_audio: bytes, stt_instance: RealtimeTranscriber | None = None) -> None:
        """
        STT → Streaming LLM → Concurrent TTS → Queue to Twilio.
        """
        nonlocal history, locked_lang, call_sid, stream_sid
        async with _processing_lock:
            # ── STT ───────────────────────────────────────────────────────────
            try:
                if stt_instance is not None:
                    # ⚡ Fast path: STT was streaming during speech — just get the result
                    transcript, stt_lang, confidence = await asyncio.to_thread(stt_instance.finish)
                    logger.info(
                        "RealtimeSTT result → %r  lang=%s  conf=%.2f",
                        transcript, stt_lang, confidence,
                    )
                    # Fallback to batch if streaming returned empty (edge case)
                    if not transcript:
                        logger.info("RealtimeSTT empty — falling back to batch STT")
                        transcript, stt_lang, confidence = await asyncio.to_thread(
                            transcribe_pcm, pcm_audio, 8000
                        )
                else:
                    transcript, stt_lang, confidence = await asyncio.to_thread(
                        transcribe_pcm, pcm_audio, 8000
                    )
            except Exception as exc:
                logger.error("STT error in media-stream: %s", exc, exc_info=True)
                return

            if not transcript:
                logger.info("VAD utterance yielded empty STT — skipping")
                return

            logger.info(
                "Media-stream STT → %r  lang=%s  conf=%.2f",
                transcript, stt_lang, confidence,
            )

            detected_lang = locked_lang or "si"
            t0 = asyncio.get_event_loop().time()
            mark_counter = 0
            
            # ── Streaming LLM ─────────────────────────────────────────────────
            try:
                stream_gen = async_stream_chat_with_tools(
                    user_message=transcript,
                    history=history,
                    stt_lang=stt_lang,
                    locked_lang=locked_lang,
                    call_sid=call_sid or ""
                )
                
                text_buffer = ""
                sentence_end_re = re.compile(r'(?<=[.!?।])\s+')
                should_escalate = False
                should_hangup = False
                tts_tasks = []
                
                async for event in stream_gen:
                    if event["type"] == "text":
                        text_buffer += str(event.get("text", ""))
                        
                        sentences = sentence_end_re.split(text_buffer)
                        if len(sentences) > 1:
                            # Process all complete sentences
                            for sentence in sentences[:-1]:
                                sent_clean = sentence.replace("[ESCALATE]", "").replace("[HANGUP]", "").strip()
                                if sent_clean:
                                    mark_counter += 1
                                    if mark_counter == 1:
                                        logger.info("⚡ LLM first sentence ready in %.2fs", asyncio.get_event_loop().time() - t0)
                                    # Create TTS task and enqueue it immediately
                                    task = asyncio.create_task(generate_tts_payloads(sent_clean, str(detected_lang), mark_counter, stream_sid or ""))
                                    tts_tasks.append(task)
                                    await audio_queue.put(task)
                            text_buffer = sentences[-1]
                            
                    elif event["type"] == "history":
                        history = event.get("history", history)  # type: ignore
                        detected_lang = str(event.get("detected_lang", detected_lang))
                        
                    elif event["type"] == "control":
                        should_escalate = bool(event.get("escalate", False))
                        should_hangup = bool(event.get("hangup", False))
                        
                # Process remaining text in buffer
                sent_clean = text_buffer.replace("[ESCALATE]", "").replace("[HANGUP]", "").strip()
                if sent_clean:
                    mark_counter += 1
                    task = asyncio.create_task(generate_tts_payloads(sent_clean, str(detected_lang), mark_counter, stream_sid or ""))
                    tts_tasks.append(task)
                    await audio_queue.put(task)
                    
            except asyncio.CancelledError:
                logger.info("LLM stream cancelled due to barge-in.")
                raise
            except Exception as exc:
                logger.error("LLM error for call %s: %s", call_sid, exc, exc_info=True)
                # Play recovery prompt
                from src.language import REPEAT_PROMPTS
                recovery_text = REPEAT_PROMPTS.get(str(detected_lang), "Sorry, could you say that again?")
                task = asyncio.create_task(generate_tts_payloads(recovery_text, str(detected_lang), 999, stream_sid or ""))
                await audio_queue.put(task)
                return

            # Update DB Turns
            try:
                turns = []
                for i in range(0, len(history) - 1, 2):
                    user_turn = history[i] if i < len(history) else None
                    agent_turn = history[i + 1] if i + 1 < len(history) else None
                    if user_turn:
                        turns.append({"role": "user", "text": str(user_turn.get("content", "")),
                                       "lang": detected_lang,
                                       "ts": datetime.now(timezone.utc).isoformat()})
                    if agent_turn:
                        turns.append({"role": "assistant", "text": str(agent_turn.get("content", "")),
                                       "lang": detected_lang,
                                       "ts": datetime.now(timezone.utc).isoformat()})
                
                call_db.upsert_call(call_sid=call_sid or "", status="in-progress", escalated=should_escalate, turns=turns)
            except Exception as e:
                logger.error("Error saving turns to DB: %s", e)

            # ── Persist session ───────────────────────────────────────────────
            if call_sid:
                session = get_session(call_sid)
                session["history"] = history
                save_session(call_sid, session)

            logger.info(
                "Media-stream turn complete for call %s  escalate=%s  hangup=%s",
                call_sid, should_escalate, should_hangup,
            )

            # Wait for all TTS tasks to finish queuing and sending before closing
            if (should_escalate or should_hangup) and tts_tasks:
                try:
                    await asyncio.gather(*tts_tasks)
                except asyncio.CancelledError:
                    pass

            # ── Handle escalation / hangup flags ─────────────────────────────
            if should_escalate or should_hangup:
                logger.info("Closing WebSocket for call %s (escalate=%s hangup=%s)", call_sid, should_escalate, should_hangup)
                await websocket.close()

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            event = data.get("event", "")

            if event == "connected":
                logger.info("Twilio Stream connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid   = data["start"]["callSid"]
                logger.info("Stream started: %s  call: %s", stream_sid, call_sid)

                # Load session (language + history already set by /gather)
                session     = get_session(call_sid)
                locked_lang = session.get("lang") or "si"
                history     = session.get("history", [])

                # Reset VAD state for this new call
                vad.reset()
                logger.info("VAD ready for call %s (lang=%s)", call_sid, locked_lang)

                # ⚡ Fix 4: Pre-warm TTS connection — opens Google TTS TCP connection
                # so the FIRST utterance doesn't pay the cold-start penalty (~150 ms).
                async def _prewarm_tts(lang: str) -> None:
                    try:
                        await asyncio.to_thread(synthesize_mulaw, ".", lang)
                    except Exception:
                        pass  # pre-warm failure is non-fatal
                asyncio.create_task(_prewarm_tts(locked_lang))

                logger.info(
                    "Stream ready for call %s (lang=%s, lang_confirmed=%s)",
                    call_sid, locked_lang, session.get("lang_confirmed")
                )

            elif event == "media":
                # Decode the base64 MULAW chunk Twilio sends
                chunk = base64.b64decode(data["media"]["payload"])

                # Convert to PCM for realtime STT feed
                pcm_chunk = audioop.ulaw2lin(chunk, 2)

                # Track VAD state transition for STT onset detection
                was_speaking = vad.is_speaking

                # Feed to VAD — fires when utterance complete
                result = vad.process_mulaw_chunk(chunk)

                # ⚡ Start realtime STT as soon as speech begins
                if not was_speaking and vad.is_speaking:
                    _realtime_stt = RealtimeTranscriber(lang=locked_lang, sample_rate=8000)
                    _realtime_stt.start()
                    logger.debug("RealtimeSTT started for call %s", call_sid)
                    
                    # ⚡ BARGE-IN logic
                    if _current_utterance_task and not _current_utterance_task.done():
                        logger.info("Barge-in detected! Cancelling ongoing AI response.")
                        _current_utterance_task.cancel()
                        
                        # Clear any pending TTS tasks in our internal queue
                        while not audio_queue.empty():
                            try:
                                item = audio_queue.get_nowait()
                                if isinstance(item, asyncio.Task):
                                    item.cancel()
                                audio_queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                                
                        # Stop Twilio playback immediately via queue
                        if stream_sid:
                            await audio_queue.put({"event": "clear", "streamSid": stream_sid})

                # Feed PCM frames into the running STT stream
                if vad.is_speaking and _realtime_stt is not None:
                    _realtime_stt.feed(pcm_chunk)

                if result.complete:
                    logger.info(
                        "VAD: utterance captured (%d bytes PCM) for call %s",
                        len(result.pcm), call_sid,
                    )
                    # Hand the running STT instance to handle_utterance
                    stt_to_use = _realtime_stt
                    _realtime_stt = None
                    
                    # Store task so we can cancel it on barge-in
                    _current_utterance_task = asyncio.create_task(handle_utterance(result.pcm, stt_to_use))

            elif event == "stop":
                logger.info("Twilio Stream stopped for call %s", call_sid)
                break

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for call %s", call_sid)
    except Exception as exc:
        logger.error("WebSocket error for call %s: %s", call_sid, exc, exc_info=True)
    finally:
        vad.reset()
        # Clean up tasks
        sender_task.cancel()
        if _current_utterance_task and not _current_utterance_task.done():
            _current_utterance_task.cancel()
        logger.info("WebSocket handler cleaned up for call %s", call_sid)



@app.post("/status")
async def call_status(request: Request):
    """
    Twilio status callback — updates call status to completed/failed and
    cleans up the in-memory session when the call ends.
    """
    form = await request.form()
    call_sid: str = str(form.get("CallSid", "unknown"))
    status: str = str(form.get("CallStatus", ""))
    call_duration: int = int(str(form.get("CallDuration", 0) or 0))
    logger.info("Call status: %s → %s  duration=%ds", call_sid, status, call_duration)

    terminal_statuses = ("completed", "busy", "failed", "no-answer", "canceled")
    if status in terminal_statuses:
        ended_at = datetime.now(timezone.utc).isoformat()
        elapsed = int(time.time() - pop_call_start(call_sid, time.time()))
        duration = call_duration if call_duration else elapsed

        # Fetch existing record to preserve phone_number, caller_city,
        # caller_country, escalated flag, and turns — only update
        # the terminal fields (status, ended_at, duration_sec).
        existing = call_db.get_call(call_sid)
        if existing:
            call_db.upsert_call(
                call_sid=call_sid,
                phone_number=existing.get("phone_number", ""),
                caller_city=existing.get("caller_city", ""),
                caller_country=existing.get("caller_country", ""),
                status="completed" if status == "completed" else status,
                escalated=bool(existing.get("escalated", 0)),
                ended_at=ended_at,
                duration_sec=duration,
                turns=existing.get("turns", []),
            )
        else:
            # Call not in DB yet (edge case) — create a minimal record
            call_db.upsert_call(
                call_sid=call_sid,
                status="completed" if status == "completed" else status,
                ended_at=ended_at,
                duration_sec=duration,
            )
        logger.info("Call %s marked as '%s' (duration %ds)", call_sid,
                    "completed" if status == "completed" else status, duration)
        clear_session(call_sid)
    return Response(content="<Response/>", media_type="application/xml")
