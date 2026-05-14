# src/live_handler.py
"""
Gemini Multimodal Live API handler for Twilio Media Streams.

Architecture
────────────
  Twilio (8kHz MULAW) ──ulaw2lin──> resample 8k→16k PCM ──> Gemini Live
  Gemini Live (24kHz PCM) ──resample 24k→8k──> lin2ulaw ──> Twilio

Fixes applied vs original
──────────────────────────
  Fix 1 : System prompt delivered via system_instruction (not a user turn).
           A short "start" text turn is sent after stream-start to trigger
           Gemini's opening greeting.
  Fix 9 : audioop.ratecv state is threaded between calls (avoids audio
           glitches that caused STT re-processing on the old pipeline).
  Tool  : All DB tool calls run inside asyncio.to_thread so the gemini_receiver
           task is never blocked — preventing model timeouts on slow queries.
  Barge : When Gemini detects an interruption it sends server_content.interrupted=True;
          we immediately send a Twilio <Stream> "clear" event to stop playback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import audioop
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from src.session_store import get_session, save_session
from src.language import build_system_prompt
from src.llm import TOOLS
from src.db import (
    create_booking,
    create_complaint,
    get_available_slots,
    get_department_transfer_number,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool dispatcher (runs in a thread pool — never blocks the event loop)
# ---------------------------------------------------------------------------

async def _dispatch_tool(tool_name: str, tool_args: dict, call_sid: str) -> dict:
    """Execute a tool function in a thread and return its result dict."""

    def _run() -> dict:
        try:
            if tool_name == "book_appointment":
                res = create_booking(call_sid or "unknown", **tool_args)
                return {"result": f"Success. Booking ID: {res['id']}"}
            elif tool_name == "get_available_slots":
                res = get_available_slots(**tool_args)
                return {"available_slots": res}
            elif tool_name == "file_complaint":
                res = create_complaint(call_sid or "unknown", **tool_args)
                return {"result": f"Success. Complaint ID: {res['id']}"}
            elif tool_name == "transfer_to_human":
                dept_num = get_department_transfer_number(tool_args["department"])
                return {
                    "result": (
                        f"Transfer initiated to {tool_args['department']}. "
                        f"(Phone: {dept_num})"
                    )
                }
            else:
                return {"error": "Unknown tool"}
        except Exception as exc:
            logger.error("Tool execution error [%s]: %s", tool_name, exc, exc_info=True)
            return {"error": f"Error executing tool: {exc}"}

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Main WebSocket handler
# ---------------------------------------------------------------------------

async def media_stream_live(websocket: WebSocket) -> None:
    """
    Handle a Twilio Media Stream WebSocket using the Gemini Live API.

    Twilio connects here after language selection; we:
      1. Open a Gemini Live session with the correct system prompt.
      2. Forward MULAW→PCM audio chunks to Gemini in real time.
      3. Receive PCM audio from Gemini, resample, and send back as MULAW.
      4. Handle barge-in (Gemini interrupted flag) and tool calls.
    """
    await websocket.accept()
    logger.info("Live WebSocket connection established")

    stream_sid: str | None = None
    call_sid:   str | None = None
    locked_lang = "si"

    # ── Fix 9: Per-session resampler states ──────────────────────────────────
    # audioop.ratecv is stateful; threading the state avoids reconstruction
    # artefacts at chunk boundaries (clicking/popping that confuses STT).
    _in_resample_state:  object = None   # 8kHz → 16kHz (Twilio → Gemini)
    _out_resample_state: object = None   # 24kHz → 8kHz  (Gemini → Twilio)

    client = genai.Client()

    # Build a default system prompt; will be rebuilt after we learn the lang
    sys_prompt = build_system_prompt(locked_lang)

    try:
        async with client.aio.live.connect(
            model="gemini-2.0-flash-live-001",
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                tools=TOOLS,
                # ── Fix 1: Deliver the system prompt via system_instruction,
                # NOT as a user content turn. This is the correct API usage.
                system_instruction=types.Content(
                    parts=[types.Part.from_text(text=sys_prompt)]
                ),
            ),
        ) as session:
            logger.info("Connected to Gemini Live API")

            # ── Twilio receiver task ──────────────────────────────────────────

            async def twilio_receiver() -> None:
                nonlocal stream_sid, call_sid, locked_lang
                nonlocal _in_resample_state
                try:
                    while True:
                        message = await websocket.receive_text()
                        data = json.loads(message)
                        event = data.get("event")

                        if event == "start":
                            stream_sid = data["start"]["streamSid"]
                            call_sid   = data["start"]["callSid"]
                            logger.info(
                                "Live stream started: %s  call: %s",
                                stream_sid, call_sid,
                            )

                            # Load language from session (set in /gather)
                            s_data = get_session(call_sid)
                            if s_data.get("lang"):
                                locked_lang = s_data["lang"]

                            # ── Fix 1: Send a brief trigger turn so Gemini
                            # delivers its opening greeting immediately.
                            # We do NOT re-inject the system prompt here —
                            # it was already set in system_instruction above.
                            await session.send_client_content(
                                turns=[
                                    types.Content(
                                        role="user",
                                        parts=[types.Part.from_text(
                                            text="Hello, I just connected."
                                        )],
                                    )
                                ],
                                turn_complete=True,
                            )
                            logger.info(
                                "Sent greeting trigger to Gemini for call %s (lang=%s)",
                                call_sid, locked_lang,
                            )

                        elif event == "media":
                            nonlocal _in_resample_state
                            chunk = base64.b64decode(data["media"]["payload"])
                            # MULAW → 8kHz LINEAR16 PCM
                            pcm_8k = audioop.ulaw2lin(chunk, 2)
                            # ⚡ Fix 9: Thread resampler state between chunks
                            pcm_16k, _in_resample_state = audioop.ratecv(
                                pcm_8k, 2, 1, 8000, 16000, _in_resample_state
                            )
                            # Stream to Gemini
                            await session.send_realtime_input(
                                media=types.Blob(
                                    mime_type="audio/pcm;rate=16000",
                                    data=pcm_16k,
                                )
                            )

                        elif event == "stop":
                            logger.info(
                                "Twilio stream stopped for call %s", call_sid
                            )
                            break

                except WebSocketDisconnect:
                    logger.info(
                        "Twilio WebSocket disconnected for call %s", call_sid
                    )
                except Exception as exc:
                    logger.error(
                        "Error in twilio_receiver for call %s: %s",
                        call_sid, exc, exc_info=True,
                    )

            # ── Gemini receiver task ──────────────────────────────────────────

            async def gemini_receiver() -> None:
                nonlocal _out_resample_state
                try:
                    async for response in session.receive():
                        # ── Audio output ──────────────────────────────────────
                        server_content = response.server_content
                        if server_content is not None:
                            # Barge-in: Gemini was interrupted by caller speech
                            if server_content.interrupted:
                                logger.info(
                                    "Gemini interrupted (barge-in) for call %s",
                                    call_sid,
                                )
                                if stream_sid:
                                    await websocket.send_json(
                                        {"event": "clear", "streamSid": stream_sid}
                                    )
                                # Reset output resampler state on interruption
                                _out_resample_state = None

                            model_turn = server_content.model_turn
                            if model_turn:
                                for part in model_turn.parts:
                                    if part.inline_data:
                                        # Gemini outputs 24kHz LINEAR16 PCM
                                        pcm_24k = part.inline_data.data
                                        # ⚡ Fix 9: Thread output resampler state
                                        pcm_8k, _out_resample_state = audioop.ratecv(
                                            pcm_24k, 2, 1, 24000, 8000,
                                            _out_resample_state,
                                        )
                                        # 8kHz PCM → MULAW for Twilio
                                        mulaw_chunk = audioop.lin2ulaw(pcm_8k, 2)
                                        payload = base64.b64encode(mulaw_chunk).decode()
                                        if stream_sid:
                                            await websocket.send_json(
                                                {
                                                    "event": "media",
                                                    "streamSid": stream_sid,
                                                    "media": {"payload": payload},
                                                }
                                            )

                        # ── Tool call handling ────────────────────────────────
                        elif response.tool_call is not None:
                            tool_call = response.tool_call
                            logger.info(
                                "Gemini tool call for call %s: %s",
                                call_sid, tool_call,
                            )

                            function_responses = []
                            # ⚡ Fix 1 (Tool): Dispatch ALL tool calls concurrently
                            # in thread pool so we never block this async task.
                            tasks = [
                                _dispatch_tool(
                                    fc.name,
                                    dict(fc.args) if fc.args else {},
                                    call_sid or "unknown",
                                )
                                for fc in tool_call.function_calls
                            ]
                            results = await asyncio.gather(*tasks)

                            for fc, result in zip(tool_call.function_calls, results):
                                logger.info(
                                    "🔧 Tool %s → %s", fc.name, result
                                )
                                function_responses.append(
                                    types.FunctionResponse(
                                        name=fc.name,
                                        id=fc.id,
                                        response=result,
                                    )
                                )

                            await session.send_tool_response(
                                tool_responses=function_responses
                            )

                except Exception as exc:
                    logger.error(
                        "Error in gemini_receiver for call %s: %s",
                        call_sid, exc, exc_info=True,
                    )

            # ── Run both tasks; stop when either finishes ─────────────────────
            t1 = asyncio.create_task(twilio_receiver(), name="twilio_receiver")
            t2 = asyncio.create_task(gemini_receiver(), name="gemini_receiver")

            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

            # Persist history/session on clean exit
            if call_sid:
                try:
                    sess = get_session(call_sid)
                    save_session(call_sid, sess)
                except Exception:
                    pass

    except Exception as exc:
        logger.error(
            "Error connecting to Gemini Live for call %s: %s",
            call_sid, exc, exc_info=True,
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Live handler cleaned up for call %s", call_sid)
