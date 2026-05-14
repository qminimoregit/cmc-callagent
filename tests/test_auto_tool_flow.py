"""
tests/test_auto_tool_flow.py
============================
Tests the AUTOMATIC complaint and appointment creation that happens
during a live Twilio call.

Flow under test
---------------
Caller speaks  →  STT (mocked)  →  LLM / Claude (mocked)
  →  tool call (file_complaint / book_appointment)
  →  create_complaint() / create_booking() in db.py (mocked)
  →  DB record created automatically

Test groups
-----------
A. TestComplaintToolFired     — Claude fires file_complaint in EN / SI / TA
B. TestBookingToolFired       — Claude fires book_appointment in EN / SI / TA
C. TestMultiTurnDataCollection — Agent asks for missing fields before tool fires
D. TestToolErrorHandling       — DB failure is handled gracefully per language
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, call


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_COMPLAINT_INPUT_EN = {
    "service_category": "Civil Works",
    "specific_service": "Pothole",
    "description": "Large pothole near the junction causing accidents",
    "location_address": "12 Park Road, Colombo 5",
    "caller_name": "John Fernando",
    "contact_number": "0712345678",
}

_COMPLAINT_INPUT_SI = {
    "service_category": "Civil Works",
    "specific_service": "Pothole",
    "description": "ගල්කිස්සේ පාරේ ලොකු වළක් තියෙනවා",
    "location_address": "ගල්කිස්සේ පාර, කොළඹ 6",
    "caller_name": "කමල් සිල්වා",
    "contact_number": "0771234567",
}

_COMPLAINT_INPUT_TA = {
    "service_category": "Waste Management",
    "specific_service": "Missed Garbage Collection",
    "description": "குப்பை எடுக்கவில்லை மூன்று நாட்களாக",
    "location_address": "56, கல்கிஸ்ஸை வீதி, கொழும்பு 6",
    "caller_name": "நிமலா பெரேரா",
    "contact_number": "0771234567",
}

_BOOKING_INPUT_EN = {
    "service_category": "Waste Management",
    "specific_service": "Bulk Garbage Pickup",
    "appointment_date": "2026-05-10T09:00:00+05:30",
    "caller_name": "John Fernando",
    "contact_number": "0712345678",
}

_BOOKING_INPUT_SI = {
    "service_category": "Waste Management",
    "specific_service": "කසළ ගෙන යාම",
    "appointment_date": "2026-05-10T09:00:00+05:30",
    "caller_name": "කමල් සිල්වා",
    "contact_number": "0771234567",
}

_BOOKING_INPUT_TA = {
    "service_category": "Waste Management",
    "specific_service": "குப்பை அகற்றல்",
    "appointment_date": "2026-05-10T09:00:00+05:30",
    "caller_name": "நிமலா பெரேரா",
    "contact_number": "0771234567",
}


def _mock_stream_with_tool(tool_name: str, tool_input: dict,
                            follow_up_text: str = "Done."):
    """
    Build a pair of mock streaming contexts:
      - first_stream  → Claude returns a tool_use block (no text)
      - second_stream → Claude returns a text confirmation after tool result

    Returns (first_ctx, second_ctx) that can be used as
    side_effect values for messages.stream().__enter__().
    """
    # ── First stream: tool_use block ──────────────────────────────────────
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = f"toolu_{tool_name[:6]}_test"
    tool_block.name = tool_name
    tool_block.input = tool_input

    first_final = MagicMock()
    first_final.content = [tool_block]

    first_stream = MagicMock()
    first_stream.text_stream = iter([])                 # no text chunks
    first_stream.get_final_message.return_value = first_final

    first_ctx = MagicMock()
    first_ctx.__enter__ = MagicMock(return_value=first_stream)
    first_ctx.__exit__ = MagicMock(return_value=False)

    # ── Second stream: text confirmation after tool result ────────────────
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = follow_up_text

    second_final = MagicMock()
    second_final.content = [text_block]

    second_stream = MagicMock()
    second_stream.text_stream = iter([follow_up_text])
    second_stream.get_final_message.return_value = second_final

    second_ctx = MagicMock()
    second_ctx.__enter__ = MagicMock(return_value=second_stream)
    second_ctx.__exit__ = MagicMock(return_value=False)

    return first_ctx, second_ctx


def _mock_stream_text_only(text: str):
    """Build a mock stream that returns a plain text reply (no tool)."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text

    final_msg = MagicMock()
    final_msg.content = [text_block]

    stream = MagicMock()
    stream.text_stream = iter([text])
    stream.get_final_message.return_value = final_msg

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=stream)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ═════════════════════════════════════════════════════════════════════════════
# A. TestComplaintToolFired
# Claude fires file_complaint in all 3 languages
# ═════════════════════════════════════════════════════════════════════════════

class TestComplaintToolFired:

    @patch("src.llm.create_complaint", return_value={"id": 42, "status": "Open"})
    @patch("src.llm._get_client")
    def test_complaint_tool_fired_en(self, mock_client, mock_create):
        """English: Claude calls file_complaint → create_complaint called with correct args."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_EN,
            follow_up_text="Your complaint has been filed. Reference number: 42."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, history, lang, escalate = chat(
                "I want to report a pothole on Park Road.",
                [], locked_lang="en", call_sid="CA_TEST_EN_COMPLAINT"
            )

        # DB function must have been called exactly once
        mock_create.assert_called_once()

        # Positional arg 0 = call_sid
        assert mock_create.call_args[0][0] == "CA_TEST_EN_COMPLAINT"

        # Keyword args must match tool input exactly
        kwargs = mock_create.call_args[1]
        assert kwargs["service_category"] == "Civil Works"
        assert kwargs["specific_service"] == "Pothole"
        assert kwargs["caller_name"] == "John Fernando"
        assert kwargs["contact_number"] == "0712345678"
        assert "Colombo 5" in kwargs["location_address"]

        # Reply must contain the confirmation (ID returned by DB)
        assert "42" in reply
        assert not escalate

    @patch("src.llm.create_complaint", return_value={"id": 55, "status": "Open"})
    @patch("src.llm._get_client")
    def test_complaint_tool_fired_si(self, mock_client, mock_create):
        """Sinhala: Claude calls file_complaint with Sinhala caller data."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_SI,
            follow_up_text="ඔබේ පැමිණිල්ල ලියාපදිංචි කෙරිණා. අංකය 55."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, history, lang, escalate = chat(
                "පාරේ ලොකු වළක් තියෙනවා",
                [], locked_lang="si", call_sid="CA_TEST_SI_COMPLAINT"
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["service_category"] == "Civil Works"
        assert kwargs["caller_name"] == "කමල් සිල්වා"
        assert kwargs["contact_number"] == "0771234567"
        assert "55" in reply
        assert not escalate

    @patch("src.llm.create_complaint", return_value={"id": 71, "status": "Open"})
    @patch("src.llm._get_client")
    def test_complaint_tool_fired_ta(self, mock_client, mock_create):
        """Tamil: Claude calls file_complaint with Tamil caller data."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_TA,
            follow_up_text="உங்கள் புகார் பதிவு செய்யப்பட்டது. எண் 71."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, history, lang, escalate = chat(
                "என் வீதியில் குப்பை எடுக்கவில்லை",
                [], locked_lang="ta", call_sid="CA_TEST_TA_COMPLAINT"
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["service_category"] == "Waste Management"
        assert kwargs["caller_name"] == "நிமலா பெரேரா"
        assert "71" in reply
        assert not escalate

    @patch("src.llm.create_complaint", return_value={"id": 99, "status": "Open"})
    @patch("src.llm._get_client")
    def test_complaint_id_returned_in_reply(self, mock_client, mock_create):
        """The complaint ID from the DB must appear in the reply to the caller."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_EN,
            follow_up_text="Complaint logged successfully. Your reference number is 99."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, _ = chat(
                "I want to report a broken streetlight.",
                [], locked_lang="en", call_sid="CA_ID_CHECK"
            )

        assert "99" in reply

    @patch("src.llm.create_complaint", side_effect=Exception("DB connection error"))
    @patch("src.llm._get_client")
    def test_complaint_tool_db_error_handled_en(self, mock_client, mock_create):
        """If the DB raises an exception, the LLM should get an error result and still reply."""
        from src.llm import chat

        # First stream fires tool, second stream is a recovery reply
        first_ctx, _ = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_EN,
            follow_up_text="I'm sorry, there was a problem filing your complaint."
        )
        # After error, the second stream provides an apology text
        error_recovery_ctx = _mock_stream_text_only(
            "I'm sorry, there was a problem filing your complaint. Please try again."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, error_recovery_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            # Should NOT raise — error is caught and handled
            reply, _, _, escalate = chat(
                "Report a pothole.", [], locked_lang="en", call_sid="CA_ERR_EN"
            )

        # DB was called (and failed)
        mock_create.assert_called_once()
        # Should not escalate just because of a DB error
        assert not escalate


# ═════════════════════════════════════════════════════════════════════════════
# B. TestBookingToolFired
# Claude fires book_appointment in all 3 languages
# ═════════════════════════════════════════════════════════════════════════════

class TestBookingToolFired:

    @patch("src.llm.create_booking", return_value={"id": 10, "status": "Pending"})
    @patch("src.llm._get_client")
    def test_booking_tool_fired_en(self, mock_client, mock_create):
        """English: Claude calls book_appointment → create_booking called with correct args."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "book_appointment", _BOOKING_INPUT_EN,
            follow_up_text="Your appointment has been booked. Booking ID: 10."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, history, lang, escalate = chat(
                "I'd like to schedule a bulk garbage pickup.",
                [], locked_lang="en", call_sid="CA_TEST_EN_BOOKING"
            )

        mock_create.assert_called_once()
        assert mock_create.call_args[0][0] == "CA_TEST_EN_BOOKING"

        kwargs = mock_create.call_args[1]
        assert kwargs["service_category"] == "Waste Management"
        assert kwargs["specific_service"] == "Bulk Garbage Pickup"
        assert kwargs["caller_name"] == "John Fernando"
        assert kwargs["contact_number"] == "0712345678"
        assert "10" in reply
        assert not escalate

    @patch("src.llm.create_booking", return_value={"id": 20, "status": "Pending"})
    @patch("src.llm._get_client")
    def test_booking_tool_fired_si(self, mock_client, mock_create):
        """Sinhala: Claude calls book_appointment with Sinhala caller data."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "book_appointment", _BOOKING_INPUT_SI,
            follow_up_text="ඔබේ හමුවීම ලියාපදිංචි කෙරිණා. අංකය 20."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, lang, escalate = chat(
                "කසළ ගෙන යාමේ හමුවීමක් වෙන් කරන්න ඕනේ",
                [], locked_lang="si", call_sid="CA_TEST_SI_BOOKING"
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["service_category"] == "Waste Management"
        assert kwargs["caller_name"] == "කමල් සිල්වා"
        assert "20" in reply
        assert not escalate

    @patch("src.llm.create_booking", return_value={"id": 30, "status": "Pending"})
    @patch("src.llm._get_client")
    def test_booking_tool_fired_ta(self, mock_client, mock_create):
        """Tamil: Claude calls book_appointment with Tamil caller data."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "book_appointment", _BOOKING_INPUT_TA,
            follow_up_text="உங்கள் நியமனம் பதிவு செய்யப்பட்டது. எண் 30."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, lang, escalate = chat(
                "குப்பை அகற்றல் நியமனம் வேண்டும்",
                [], locked_lang="ta", call_sid="CA_TEST_TA_BOOKING"
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["caller_name"] == "நிமலா பெரேரா"
        assert "30" in reply
        assert not escalate

    @patch("src.llm.create_booking", return_value={"id": 15, "status": "Pending"})
    @patch("src.llm._get_client")
    def test_booking_appointment_date_stored_correctly(self, mock_client, mock_create):
        """Appointment date must be passed through to create_booking as provided."""
        from src.llm import chat

        first_ctx, second_ctx = _mock_stream_with_tool(
            "book_appointment", _BOOKING_INPUT_EN,
            follow_up_text="Appointment booked for 10 May at 9 AM. ID: 15."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            chat("Book a garbage pickup for Friday.", [],
                 locked_lang="en", call_sid="CA_DATE_CHECK")

        kwargs = mock_create.call_args[1]
        assert kwargs["appointment_date"] == "2026-05-10T09:00:00+05:30"

    @patch("src.llm.create_booking", side_effect=Exception("DB timeout"))
    @patch("src.llm._get_client")
    def test_booking_tool_db_error_handled(self, mock_client, mock_create):
        """If DB raises, the error is caught and caller gets an apology, not a crash."""
        from src.llm import chat

        first_ctx, _ = _mock_stream_with_tool(
            "book_appointment", _BOOKING_INPUT_EN,
            follow_up_text="Sorry, I couldn't complete your booking."
        )
        recovery_ctx = _mock_stream_text_only(
            "I'm sorry, I was unable to book your appointment. Please call back."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, recovery_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, escalate = chat(
                "Book an appointment.", [], locked_lang="en", call_sid="CA_BOOK_ERR"
            )

        mock_create.assert_called_once()
        assert not escalate   # DB error should not trigger escalation


# ═════════════════════════════════════════════════════════════════════════════
# C. TestMultiTurnDataCollection
# Agent asks for missing fields before calling the tool
# ═════════════════════════════════════════════════════════════════════════════

class TestMultiTurnDataCollection:
    """
    These tests verify that the agent does NOT call the tool prematurely.
    When the caller provides an incomplete request, the agent should ask
    for the missing info — the tool should NOT be called yet.
    """

    @patch("src.llm.create_complaint")
    @patch("src.llm._get_client")
    def test_tool_not_called_on_first_mention_en(self, mock_client, mock_create):
        """English: 'I have a complaint' alone must NOT trigger file_complaint."""
        from src.llm import chat

        # Claude returns a question (asking for more info), not a tool call
        ctx = _mock_stream_text_only(
            "I'd be happy to help. Could you describe the issue and its location?"
        )
        mock_client.return_value.messages.stream.return_value = ctx

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, escalate = chat(
                "I have a complaint.",
                [], locked_lang="en", call_sid="CA_MULTI_EN"
            )

        # Tool must NOT have fired yet
        mock_create.assert_not_called()
        # Agent must ask a follow-up question
        assert len(reply) > 5
        assert not escalate

    @patch("src.llm.create_complaint")
    @patch("src.llm._get_client")
    def test_tool_not_called_on_first_mention_si(self, mock_client, mock_create):
        """Sinhala: partial request does NOT trigger tool."""
        from src.llm import chat

        ctx = _mock_stream_text_only(
            "ඒ ගැන පැමිණිල්ලක් ගන්නම්. ලිපිනය කියන්නෙ?"
        )
        mock_client.return_value.messages.stream.return_value = ctx

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, _ = chat(
                "පාරේ ලොකු වළක් තියෙනවා",
                [], locked_lang="si", call_sid="CA_MULTI_SI"
            )

        mock_create.assert_not_called()
        assert len(reply) > 5

    @patch("src.llm.create_complaint")
    @patch("src.llm._get_client")
    def test_tool_not_called_on_first_mention_ta(self, mock_client, mock_create):
        """Tamil: partial request does NOT trigger tool."""
        from src.llm import chat

        ctx = _mock_stream_text_only(
            "புகார் பதிவு செய்கிறேன். முகவரியைச் சொல்லுங்கள்."
        )
        mock_client.return_value.messages.stream.return_value = ctx

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, _ = chat(
                "என் வீதியில் குப்பை எடுக்கவில்லை",
                [], locked_lang="ta", call_sid="CA_MULTI_TA"
            )

        mock_create.assert_not_called()
        assert len(reply) > 5

    @patch("src.llm.create_booking")
    @patch("src.llm._get_client")
    def test_booking_not_called_without_date_en(self, mock_client, mock_create):
        """English: 'I want an appointment' without date does NOT call book_appointment."""
        from src.llm import chat

        ctx = _mock_stream_text_only(
            "Of course. What date and time works best for you?"
        )
        mock_client.return_value.messages.stream.return_value = ctx

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, _ = chat(
                "I want to book an appointment.",
                [], locked_lang="en", call_sid="CA_BOOK_NO_DATE"
            )

        mock_create.assert_not_called()
        assert len(reply) > 5

    @patch("src.llm.create_complaint", return_value={"id": 88, "status": "Open"})
    @patch("src.llm._get_client")
    def test_tool_fires_after_all_fields_collected_en(self, mock_client, mock_create):
        """
        English: after a full multi-turn exchange with all fields confirmed,
        the final turn triggers file_complaint.
        """
        from src.llm import chat

        # Simulate a pre-existing history where all info was gathered
        history = [
            {"role": "user",      "content": "I want to report a pothole on Park Road."},
            {"role": "assistant", "content": "Could you describe the issue?"},
            {"role": "user",      "content": "Big pothole near the junction."},
            {"role": "assistant", "content": "Address is 12 Park Road, Colombo 5 — correct?"},
            {"role": "user",      "content": "Yes."},
            {"role": "assistant", "content": "Your name please?"},
            {"role": "user",      "content": "John Fernando"},
            {"role": "assistant", "content": "I have your name as John Fernando, correct?"},
            {"role": "user",      "content": "Yes."},
            {"role": "assistant", "content": "And your contact number?"},
            {"role": "user",      "content": "0712345678"},
            {"role": "assistant", "content": "I have 0-7-1-2-3-4-5-6-7-8, is that correct?"},
        ]

        # Final user message confirms the number — Claude now fires the tool
        first_ctx, second_ctx = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_EN,
            follow_up_text="Your complaint has been logged. Reference 88."
        )
        mock_client.return_value.messages.stream.side_effect = [first_ctx, second_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, escalate = chat(
                "Yes that's correct.",
                history, locked_lang="en", call_sid="CA_FULL_FLOW"
            )

        mock_create.assert_called_once()
        assert "88" in reply
        assert not escalate


# ═════════════════════════════════════════════════════════════════════════════
# D. TestToolErrorHandling — robust error cases per language
# ═════════════════════════════════════════════════════════════════════════════

class TestToolErrorHandling:

    @pytest.mark.parametrize("locked_lang,user_msg,follow_up", [
        ("en", "Report a broken drain.", "Sorry, there was a technical issue."),
        ("si", "ජලය ගලන්නේ නෑ", "කණගාටුයි, දෝෂයක් ඇතිවිය."),
        ("ta", "குப்பை பிரச்சனை", "மன்னிக்கவும், தொழில்நுட்ப பிழை."),
    ])
    @patch("src.llm.create_complaint", side_effect=Exception("PostgreSQL error"))
    @patch("src.llm._get_client")
    def test_complaint_db_failure_all_langs(
        self, mock_client, mock_create, locked_lang, user_msg, follow_up
    ):
        """DB failure on create_complaint is caught for all 3 languages."""
        from src.llm import chat

        first_ctx, _ = _mock_stream_with_tool(
            "file_complaint", _COMPLAINT_INPUT_EN, follow_up_text=follow_up
        )
        recovery_ctx = _mock_stream_text_only(follow_up)
        mock_client.return_value.messages.stream.side_effect = [first_ctx, recovery_ctx]

        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            # Must not raise
            reply, _, _, escalate = chat(
                user_msg, [], locked_lang=locked_lang,
                call_sid=f"CA_ERR_{locked_lang.upper()}"
            )

        mock_create.assert_called_once()
        assert not escalate  # Technical error ≠ escalation
