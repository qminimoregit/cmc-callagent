# src/llm.py
from __future__ import annotations
"""
Google Gemini 2.5 Flash integration — trilingual chat for CMC Assistant.
Handles language detection, system prompt construction, escalation detection,
and executes tool calls for bookings, complaints, and human transfers.
"""

import logging
import os
import re
import time
from typing import Generator

from google import genai
from google.genai import types
from dotenv import load_dotenv

from src.language import detect_language, build_system_prompt
from src.db import create_booking, create_complaint, get_department_transfer_number, get_available_slots

load_dotenv()

logger = logging.getLogger(__name__)

_client: genai.Client | None = None

# ⚡ Cap history to prevent latency growth over long calls (Fix 4)
MAX_HISTORY_MESSAGES = 20  # keep last 10 turns (user + model pairs)


def _get_client() -> genai.Client:
    """Lazy-initialise the Gemini client (respects late .env loading)."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in the environment.")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Tool definitions in Gemini format
# ---------------------------------------------------------------------------
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="book_appointment",
                description="Book an appointment for Municipal Council services (e.g. Waste Management, Public Health).",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "service_category": types.Schema(
                            type=types.Type.STRING,
                            enum=["Waste Management", "Public Health", "Civil Works", "Tax and Revenue", "Community Services"],
                        ),
                        "specific_service": types.Schema(
                            type=types.Type.STRING,
                            description="The specific service being requested",
                        ),
                        "appointment_date": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "The appointment date and time in ISO 8601 format with timezone offset "
                                "(e.g. '2026-05-16T09:00:00+05:30'). "
                                "Use CURRENT DATE AND TIME from the system prompt to resolve relative phrases "
                                "like 'next Friday' or 'tomorrow morning' into an exact ISO timestamp."
                            ),
                        ),
                        "caller_name": types.Schema(
                            type=types.Type.STRING,
                            description="Name of the caller",
                        ),
                        "contact_number": types.Schema(
                            type=types.Type.STRING,
                            description="Phone number of the caller",
                        ),
                    },
                    required=["service_category", "specific_service", "appointment_date", "caller_name", "contact_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="file_complaint",
                description="File a complaint for Municipal Council issues (e.g. potholes, dengue mosquitoes).",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "service_category": types.Schema(
                            type=types.Type.STRING,
                            enum=["Waste Management", "Public Health", "Civil Works", "Tax and Revenue", "Community Services"],
                        ),
                        "specific_service": types.Schema(
                            type=types.Type.STRING,
                            description="The specific issue or complaint type",
                        ),
                        "description": types.Schema(
                            type=types.Type.STRING,
                            description="Detailed description of the issue",
                        ),
                        "location_address": types.Schema(
                            type=types.Type.STRING,
                            description="Address or location of the issue",
                        ),
                        "caller_name": types.Schema(
                            type=types.Type.STRING,
                            description="Name of the caller",
                        ),
                        "contact_number": types.Schema(
                            type=types.Type.STRING,
                            description="Phone number of the caller",
                        ),
                    },
                    required=["service_category", "specific_service", "description", "location_address", "caller_name", "contact_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="transfer_to_human",
                description="Transfer the call to a live human agent at a specific department.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "department": types.Schema(
                            type=types.Type.STRING,
                            enum=["Waste Management", "Public Health", "Civil Works", "Tax and Revenue", "Community Services"],
                        ),
                        "reason": types.Schema(
                            type=types.Type.STRING,
                            description="Reason for the transfer",
                        ),
                    },
                    required=["department", "reason"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Query available appointment slots for a given department and date. "
                    "Call this BEFORE confirming a time with the caller, to ensure you only "
                    "offer valid available times."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "department": types.Schema(
                            type=types.Type.STRING,
                            enum=["Waste Management", "Public Health", "Civil Works", "Tax and Revenue", "Community Services"],
                        ),
                        "date": types.Schema(
                            type=types.Type.STRING,
                            description="The date to check in ISO format (YYYY-MM-DD)",
                        ),
                    },
                    required=["department", "date"],
                ),
            ),
        ]
    ),
]


def _clean_reply(text: str, max_sentences: int = 3) -> str:
    """
    Strip markdown and limit sentence count for voice output.
    Ensures replies are clean for TTS and appropriately short for phone calls.
    Also strips any incomplete trailing sentence to avoid garbled TTS audio
    when the LLM response is cut off mid-word (sha sha sha issue).
    """
    # Remove markdown formatting
    text = re.sub(r'[*_~`#]', '', text)
    # Remove numbered lists
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points
    text = re.sub(r'^[-•]\s+', '', text, flags=re.MULTILINE)
    # Remove extra whitespace / newlines
    text = re.sub(r'\s+', ' ', text).strip()
    # Limit sentences (split on . ! ? and Sinhala/Tamil danda ።)
    sentences = re.split(r'(?<=[.!?।])\s+', text.strip())
    if len(sentences) > max_sentences:
        text = ' '.join(sentences[:max_sentences])
    else:
        text = ' '.join(sentences)

    # ── Safety: strip any incomplete trailing sentence ──────────────────────
    # If the text does not end with sentence-ending punctuation, the LLM was
    # cut off mid-sentence. Trim back to the last complete sentence so TTS
    # does not produce garbled audio ("sha sha sha").
    if text and text[-1] not in '.!?।':
        # Find the last complete sentence boundary
        last_boundary = max(
            text.rfind('. '),
            text.rfind('? '),
            text.rfind('! '),
            text.rfind('। '),
        )
        if last_boundary > 0:
            # Keep up to and including the punctuation
            text = text[:last_boundary + 1].strip()
        # If no boundary found, keep the full text as-is (single sentence)

    return text.strip()


def _trim_history(history: list[dict]) -> list[dict]:
    """
    Cap conversation history to MAX_HISTORY_MESSAGES to prevent latency
    growth over long calls (Fix 4).

    Gemini uses 'user' and 'model' roles in its history.
    """
    if len(history) > MAX_HISTORY_MESSAGES:
        history = history[-MAX_HISTORY_MESSAGES:]
        # Ensure history starts with a 'user' message (Gemini requirement)
        while history and history[0].get("role") != "user":
            history = history[1:]
    return history


def _safe_parts(response) -> list:
    """
    Safely extract parts from a Gemini response.

    Gemini can return a candidate with content=None or parts=None when:
    - The response is safety-blocked
    - finish_reason is MAX_TOKENS / SAFETY / RECITATION
    - The model produces a pure function-call with no text part

    Returns an empty list instead of raising TypeError.
    """
    try:
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None:
            logger.warning("Gemini returned no candidates")
            return []
        # Log finish reason to aid debugging
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason and str(finish_reason) not in ("STOP", "FinishReason.STOP", "1"):
            logger.warning("Gemini finish_reason=%s — may have truncated output", finish_reason)
        content = candidate.content
        if content is None:
            logger.warning("Gemini candidate.content is None (finish_reason=%s)", finish_reason)
            return []
        parts = content.parts
        if parts is None:
            logger.warning("Gemini content.parts is None (finish_reason=%s)", finish_reason)
            return []
        return list(parts)
    except Exception as exc:
        logger.error("_safe_parts() unexpected error: %s", exc, exc_info=True)
        return []


def _history_to_gemini(history: list[dict]) -> list[types.Content]:
    """
    Convert internal history format to Gemini Content objects.

    Internal format: [{"role": "user"/"model", "content": "..."}, ...]
    Some entries may have structured content (list of parts) from tool calls.
    """
    contents = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=content)],
                )
            )
        elif isinstance(content, list):
            # Already structured parts (e.g. tool results) — pass through
            parts = []
            for part_data in content:
                if isinstance(part_data, types.Part):
                    parts.append(part_data)
                elif isinstance(part_data, dict):
                    if "text" in part_data:
                        parts.append(types.Part.from_text(text=part_data["text"]))
                    elif "function_call" in part_data:
                        parts.append(types.Part.from_function_call(
                            name=part_data["function_call"]["name"],
                            args=part_data["function_call"]["args"],
                        ))
                    elif "function_response" in part_data:
                        parts.append(types.Part.from_function_response(
                            name=part_data["function_response"]["name"],
                            response=part_data["function_response"]["response"],
                        ))
            if parts:
                contents.append(types.Content(role=role, parts=parts))
    return contents


def chat(
    user_message: str,
    history: list[dict],
    stt_lang: str = "",
    locked_lang: str | None = None,
    call_sid: str = "local_test",
) -> tuple[str, list[dict], str, bool, bool]:
    """
    Send a user message to Gemini 2.5 Flash and receive a reply.
    Handles tool calls for bookings, complaints, and transfers.

    Returns: (reply_text, history, detected_lang, should_escalate, should_hangup)
    """
    client = _get_client()

    if locked_lang and locked_lang in ("si", "ta", "en"):
        detected_lang = locked_lang
    else:
        detected_lang = detect_language(user_message, stt_hint=stt_lang)

    system_prompt = build_system_prompt(detected_lang)

    # 2. Add user message to history
    history = history + [{"role": "user", "content": user_message}]
    logger.info("LLM → History length before trim: %d", len(history))

    # ⚡ Trim history to prevent latency growth (Fix 4)
    history = _trim_history(history)
    logger.info("LLM → History length after trim: %d", len(history))
    for i, msg in enumerate(history):
        logger.info("  [%d] %s: %s", i, msg.get("role"), str(msg.get("content"))[:50])

    logger.info("LLM → lang=%s user=%r", detected_lang, user_message[:80])

    from src.dashboard_api import _llm_params
    max_tokens = _llm_params.get("max_tokens", 500)
    temperature = _llm_params.get("temperature", 0.3)

    t0 = time.monotonic()

    # Convert history to Gemini format
    gemini_contents = _history_to_gemini(history)

    # Generate response with Gemini 2.5 Flash
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=gemini_contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=TOOLS,
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )

    logger.info("LLM first response in %.2fs", time.monotonic() - t0)

    should_escalate = False
    should_hangup = False
    reply_text = ""

    # Process the response using the safe helper — prevents TypeError when
    # content or parts is None (e.g. safety block, MAX_TOKENS finish reason)
    function_calls = []
    model_parts = []

    for part in _safe_parts(response):
        if part.text:
            reply_text += part.text
            if "[ESCALATE]" in part.text:
                should_escalate = True
            model_parts.append({"text": part.text})
        elif part.function_call:
            fc = part.function_call
            function_calls.append(fc)
            model_parts.append({
                "function_call": {
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                }
            })

    if "[HANGUP]" in reply_text:
        should_hangup = True

    # Add model response to history
    history.append({"role": "model", "content": model_parts if model_parts else reply_text})

    # Clean up markers
    reply_text = reply_text.replace("[ESCALATE]", "").replace("[HANGUP]", "").strip()

    # If tools were called, execute them and make a follow-up call
    if function_calls:
        function_responses = []
        for fc in function_calls:
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}

            logger.info(
                "🔧 Tool fired: %s  lang=%s  call=%s  args=%s",
                tool_name, detected_lang, call_sid,
                {k: v for k, v in tool_args.items() if k != "contact_number"},
            )

            try:
                if tool_name == "book_appointment":
                    res = create_booking(call_sid, **tool_args)
                    logger.info(
                        "✅ Booking created: id=%d  call=%s  category=%s  service=%s",
                        res['id'], call_sid,
                        tool_args.get('service_category', ''),
                        tool_args.get('specific_service', ''),
                    )
                    function_responses.append({
                        "function_response": {
                            "name": tool_name,
                            "response": {"result": f"Success. Booking ID: {res['id']}"},
                        }
                    })

                elif tool_name == "get_available_slots":
                    res = get_available_slots(**tool_args)
                    logger.info(
                        "🔍 Slots queried: dept=%s  date=%s  slots=%s",
                        tool_args.get('department', ''),
                        tool_args.get('date', ''),
                        res
                    )
                    function_responses.append({
                        "function_response": {
                            "name": tool_name,
                            "response": {"available_slots": res},  # type: ignore
                        }
                    })

                elif tool_name == "file_complaint":
                    res = create_complaint(call_sid, **tool_args)
                    logger.info(
                        "✅ Complaint created: id=%d  call=%s  category=%s  service=%s",
                        res['id'], call_sid,
                        tool_args.get('service_category', ''),
                        tool_args.get('specific_service', ''),
                    )
                    function_responses.append({
                        "function_response": {
                            "name": tool_name,
                            "response": {"result": f"Success. Complaint ID: {res['id']}"},
                        }
                    })

                elif tool_name == "transfer_to_human":
                    should_escalate = True
                    dept_num = get_department_transfer_number(tool_args["department"])
                    logger.info(
                        "📞 Transfer to human: dept=%s  call=%s  phone=%s",
                        tool_args['department'], call_sid, dept_num,
                    )
                    function_responses.append({
                        "function_response": {
                            "name": tool_name,
                            "response": {"result": f"Transfer initiated to {tool_args['department']}. (Phone: {dept_num})"},
                        }
                    })

            except Exception as e:
                logger.error(
                    "❌ Tool execution failed: tool=%s  call=%s  error=%s",
                    tool_name, call_sid, e,
                )
                function_responses.append({
                    "function_response": {
                        "name": tool_name,
                        "response": {"error": f"Error executing tool: {str(e)}"},
                    }
                })

        # Add tool results to history
        history.append({
            "role": "user",
            "content": function_responses
        })

        # Second call to get the final response text after tool execution
        t1 = time.monotonic()
        gemini_contents_2 = _history_to_gemini(history)

        response2 = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=gemini_contents_2,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=TOOLS,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        logger.info("LLM tool follow-up in %.2fs", time.monotonic() - t1)

        final_text = ""
        final_parts = []
        for part in _safe_parts(response2):
            if part.text:
                final_text += part.text
                final_parts.append({"text": part.text})

        history.append({"role": "model", "content": final_parts if final_parts else final_text})
        reply_text = final_text.replace("[ESCALATE]", "").replace("[HANGUP]", "").strip()
        if "[ESCALATE]" in final_text:
            should_escalate = True
        if "[HANGUP]" in final_text:
            should_hangup = True

    # Guard: if LLM output a raw JSON string of a function call as text, clear it
    if '"function_call"' in reply_text or "function_response" in reply_text:
        logger.warning("LLM hallucinated JSON string as text! Clearing reply_text to trigger recovery.")
        reply_text = ""

    # Guard: if reply is still empty, make a recovery call
    if not reply_text.strip():
        logger.warning("LLM returned empty reply — making recovery call")
        recovery_prompt = (
            "You sent an empty reply. This is NOT allowed — you MUST always respond with natural spoken text. "
            "If a tool was just successfully executed in the previous turn, you MUST politely inform the caller that their request (e.g. complaint or booking) was completed successfully. "
            "If you are still collecting details, ask for the next missing field. "
            "NEVER reply with empty text and NEVER output raw JSON."
        )
        recovery_contents = _history_to_gemini(
            history + [{"role": "user", "content": recovery_prompt}]
        )
        recovery_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=recovery_contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        for part in _safe_parts(recovery_response):
            if part.text:
                reply_text += part.text
        reply_text = reply_text.replace("[ESCALATE]", "").replace("[HANGUP]", "").strip()
        if "[HANGUP]" in reply_text:
            should_hangup = True

    # ⚡ Post-process: clean reply for voice output (Fix 7)
    reply_text = _clean_reply(reply_text)

    logger.info("LLM ← reply=%r escalate=%s hangup=%s", reply_text[:80], should_escalate, should_hangup)
    return reply_text, history, detected_lang, should_escalate, should_hangup


def stream_chat(
    user_message: str,
    history: list[dict],
    stt_lang: str = "",
    locked_lang: str | None = None,
) -> Generator[str, None, None]:
    """
    Generator version of chat() that yields text chunks.
    Note: Tool calls are NOT supported in this streaming version for simplicity,
    as they require multiple round-trips.
    """
    client = _get_client()

    if locked_lang and locked_lang in ("si", "ta", "en"):
        detected_lang = locked_lang
    else:
        detected_lang = detect_language(user_message, stt_hint=stt_lang)

    system_prompt = build_system_prompt(detected_lang)
    
    # Trim and prepare history
    history = _trim_history(history + [{"role": "user", "content": user_message}])
    gemini_contents = _history_to_gemini(history)

    from src.dashboard_api import _llm_params
    max_tokens = _llm_params.get("max_tokens", 500)
    temperature = _llm_params.get("temperature", 0.3)

    # Use streaming generation — thinking disabled for fast first-token latency
    responses = client.models.generate_content_stream(
        model="gemini-2.5-flash",
        contents=gemini_contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    for response in responses:
        for part in _safe_parts(response):
            if part.text:
                yield part.text


async def async_stream_chat_with_tools(
    user_message: str,
    history: list[dict],
    stt_lang: str = "",
    locked_lang: str | None = None,
    call_sid: str = "local_test",
):
    """
    Asynchronous streaming generator that supports tool calls.
    Yields dictionaries with events:
      - {"type": "text", "text": "chunk"}
      - {"type": "history", "history": [...], "detected_lang": "si"}
      - {"type": "control", "escalate": bool, "hangup": bool}
    """
    client = _get_client()

    if locked_lang and locked_lang in ("si", "ta", "en"):
        detected_lang = locked_lang
    else:
        detected_lang = detect_language(user_message, stt_hint=stt_lang)

    system_prompt = build_system_prompt(detected_lang)

    history = history + [{"role": "user", "content": user_message}]
    history = _trim_history(history)
    
    gemini_contents = _history_to_gemini(history)

    from src.dashboard_api import _llm_params
    max_tokens = _llm_params.get("max_tokens", 500)
    temperature = _llm_params.get("temperature", 0.3)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=TOOLS,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    should_escalate = False
    should_hangup = False
    full_text = ""
    model_parts = []
    function_calls = []

    # First stream
    actual_stream = await client.aio.models.generate_content_stream(
        model="gemini-2.5-flash",
        contents=gemini_contents,
        config=config,
    )
    
    async for response in actual_stream:
        for part in _safe_parts(response):
            if part.text:
                full_text += part.text
                model_parts.append({"text": part.text})
                # Yield text chunk directly
                yield {"type": "text", "text": part.text}
                if "[ESCALATE]" in part.text:
                    should_escalate = True
                if "[HANGUP]" in part.text:
                    should_hangup = True
            elif part.function_call:
                fc = part.function_call
                function_calls.append(fc)
                model_parts.append({
                    "function_call": {
                        "name": fc.name,
                        "args": dict(fc.args) if fc.args else {},
                    }
                })

    history.append({"role": "model", "content": model_parts if model_parts else full_text})

    # If tools were called, execute them and start a second stream
    if function_calls:
        function_responses = []
        for fc in function_calls:
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}
            
            logger.info("🔧 Tool fired: %s  lang=%s  call=%s", tool_name, detected_lang, call_sid)
            
            try:
                if tool_name == "book_appointment":
                    res = create_booking(call_sid, **tool_args)
                    function_responses.append({"function_response": {"name": tool_name, "response": {"result": f"Success. Booking ID: {res['id']}"}}})
                elif tool_name == "get_available_slots":
                    res = get_available_slots(**tool_args)
                    function_responses.append({"function_response": {"name": tool_name, "response": {"available_slots": res}}})  # type: ignore
                elif tool_name == "file_complaint":
                    res = create_complaint(call_sid, **tool_args)
                    function_responses.append({"function_response": {"name": tool_name, "response": {"result": f"Success. Complaint ID: {res['id']}"}}})
                elif tool_name == "transfer_to_human":
                    should_escalate = True
                    dept_num = get_department_transfer_number(tool_args["department"])
                    function_responses.append({"function_response": {"name": tool_name, "response": {"result": f"Transfer initiated to {tool_args['department']}. (Phone: {dept_num})" }}})
            except Exception as e:
                logger.error("❌ Tool execution failed: tool=%s error=%s", tool_name, e)
                function_responses.append({"function_response": {"name": tool_name, "response": {"error": f"Error executing tool: {str(e)}" }}})
        
        history.append({"role": "user", "content": function_responses})
        
        gemini_contents_2 = _history_to_gemini(history)
        full_text_2 = ""
        model_parts_2 = []
        
        actual_stream_2 = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=gemini_contents_2,
            config=config,
        )
        
        async for response2 in actual_stream_2:
            for part in _safe_parts(response2):
                if part.text:
                    full_text_2 += part.text
                    model_parts_2.append({"text": part.text})
                    yield {"type": "text", "text": part.text}
                    if "[ESCALATE]" in part.text:
                        should_escalate = True
                    if "[HANGUP]" in part.text:
                        should_hangup = True
        
        history.append({"role": "model", "content": model_parts_2 if model_parts_2 else full_text_2})
        full_text = full_text_2

    # Guard: if LLM output a raw JSON string of a function call as text, clear it
    if '"function_call"' in full_text or "function_response" in full_text:
        logger.warning("LLM hallucinated JSON string as text! Clearing text to trigger recovery.")
        full_text = ""

    if not full_text.strip():
        logger.warning("LLM returned empty reply — making recovery call")
        recovery_prompt = (
            "You sent an empty reply. This is NOT allowed — you MUST always respond with natural spoken text. "
            "If a tool was just successfully executed in the previous turn, you MUST politely inform the caller that their request (e.g. complaint or booking) was completed successfully. "
            "If you are still collecting details, ask for the next missing field. "
            "NEVER reply with empty text and NEVER output raw JSON."
        )
        recovery_contents = _history_to_gemini(
            history + [{"role": "user", "content": recovery_prompt}]
        )
        
        actual_stream_recovery = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=recovery_contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        
        async for response in actual_stream_recovery:
            for part in _safe_parts(response):
                if part.text:
                    full_text += part.text
                    yield {"type": "text", "text": part.text}
                    if "[ESCALATE]" in part.text:
                        should_escalate = True
                    if "[HANGUP]" in part.text:
                        should_hangup = True

    yield {"type": "history", "history": history, "detected_lang": detected_lang}
    yield {"type": "control", "escalate": should_escalate, "hangup": should_hangup}

