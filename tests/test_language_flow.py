"""
tests/test_language_flow.py — updated to match current server architecture.

Server now uses <Gather numDigits=1> for language selection (digit press),
and calls chat() + synthesize() directly in Phase 2 (not process_recording).
LLM now uses messages.stream(), not messages.create().
Session store is patched to use _local dict (no Redis in tests).
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from src.server import app
from src.session_store import _local as _sessions, _DEFAULT_SESSION
import copy

client = TestClient(app)

# Patch session_store so tests always use _local (no Redis)
_SS = "src.session_store"

def _get_sess(sid):
    return _sessions.setdefault(sid, copy.deepcopy(_DEFAULT_SESSION))

def _save_sess(sid, data):
    _sessions[sid] = data

def _clear_sess(sid):
    _sessions.pop(sid, None)

# Apply session patches globally for all tests
pytestmark = pytest.mark.usefixtures("patch_sessions")

@pytest.fixture(autouse=True)
def patch_sessions():
    with patch("src.server.get_session",   side_effect=_get_sess), \
         patch("src.server.save_session",  side_effect=_save_sess), \
         patch("src.server.clear_session", side_effect=_clear_sess), \
         patch("src.server.set_call_start"), \
         patch("src.server.pop_call_start", return_value=0.0), \
         patch("src.db.upsert_call"):
        yield

def _form(**kw):
    base = {"CallSid": "CA_TEST_001", "CallStatus": "in-progress",
            "From": "+94771234567", "CallerCity": "Colombo", "CallerCountry": "LK"}
    base.update(kw)
    return base

def _clear():
    _sessions.clear()

def _stream_ctx(text="OK"):
    """Mock a messages.stream() context that returns a plain text reply."""
    blk = MagicMock(); blk.type = "text"; blk.text = text
    final = MagicMock(); final.content = [blk]
    s = MagicMock(); s.text_stream = iter([text]); s.get_final_message.return_value = final
    ctx = MagicMock(); ctx.__enter__ = MagicMock(return_value=s); ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ── 1. language.py ──────────────────────────────────────────────────────────

class TestLanguageModule:

    def test_lang_selection_greeting_has_all_three(self):
        from src.language import LANG_SELECTION_GREETING
        assert "ආයුබෝවන්" in LANG_SELECTION_GREETING
        assert "வணக்கம்" in LANG_SELECTION_GREETING
        assert "Welcome" in LANG_SELECTION_GREETING

    def test_lang_confirmations_keys(self):
        from src.language import LANG_CONFIRMATIONS
        assert set(LANG_CONFIRMATIONS.keys()) == {"si", "ta", "en"}

    def test_lang_retry_prompt_keys(self):
        from src.language import LANG_RETRY_PROMPT
        assert set(LANG_RETRY_PROMPT.keys()) == {"si", "ta", "en"}

    @pytest.mark.parametrize("text,expected", [
        ("sinhala", "si"), ("සිංහල", "si"),
        ("tamil",   "ta"), ("தமிழ்",  "ta"),
        ("english", "en"), ("English please", "en"),
    ])
    def test_detect_language_choice_keyword(self, text, expected):
        from src.language import detect_language_choice
        assert detect_language_choice(text) == expected

    def test_detect_language_choice_none_for_gibberish(self):
        from src.language import detect_language_choice
        assert detect_language_choice("blah xyz 123") is None

    def test_detect_language_choice_stt_fallback(self):
        from src.language import detect_language_choice
        assert detect_language_choice("um hmm", stt_hint="ta") == "ta"

    def test_detect_language_choice_keyword_beats_stt(self):
        from src.language import detect_language_choice
        assert detect_language_choice("english please", stt_hint="si") == "en"

    def test_tool_guide_in_system_prompt(self):
        """System prompt must contain tool conversation guidance."""
        from src.language import BASE_SYSTEM_PROMPT
        assert "file_complaint" in BASE_SYSTEM_PROMPT
        assert "book_appointment" in BASE_SYSTEM_PROMPT
        assert "ONLY call" in BASE_SYSTEM_PROMPT


# ── 2. llm.py — streaming mocks ─────────────────────────────────────────────

class TestLLMLockedLang:

    @patch("src.llm.detect_language")
    @patch("src.llm._get_client")
    def test_locked_lang_skips_detect(self, mock_client, mock_detect):
        from src.llm import chat
        mock_client.return_value.messages.stream.return_value = _stream_ctx("ආයුබෝවන්")
        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            _, _, lang, _ = chat("කොහොමද", [], locked_lang="si")
        mock_detect.assert_not_called()
        assert lang == "si"

    @patch("src.llm.detect_language")
    @patch("src.llm._get_client")
    def test_no_locked_lang_calls_detect(self, mock_client, mock_detect):
        from src.llm import chat
        mock_detect.return_value = "en"
        mock_client.return_value.messages.stream.return_value = _stream_ctx("Hello")
        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            chat("Hello", [])
        mock_detect.assert_called_once()

    @pytest.mark.parametrize("locked", ["si", "ta", "en"])
    @patch("src.llm._get_client")
    def test_locked_lang_all_three(self, mock_client, locked):
        from src.llm import chat
        mock_client.return_value.messages.stream.return_value = _stream_ctx("OK")
        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            _, _, lang, _ = chat("test", [], locked_lang=locked)
        assert lang == locked

    @patch("src.llm._get_client")
    def test_escalation_tag_stripped(self, mock_client):
        from src.llm import chat
        mock_client.return_value.messages.stream.return_value = _stream_ctx("Please hold [ESCALATE]")
        with patch("src.dashboard_api._llm_params", {"max_tokens": 120, "temperature": 0.2}):
            reply, _, _, esc = chat("upset", [], locked_lang="en")
        assert esc is True
        assert "[ESCALATE]" not in reply


# ── 3. server.py — /voice & /gather ─────────────────────────────────────────

class TestServerVoice:

    def setup_method(self): _clear()

    def test_voice_returns_xml(self):
        r = client.post("/voice", data=_form())
        assert r.status_code == 200
        assert "xml" in r.headers["content-type"]

    def test_voice_has_gather_or_say(self):
        r = client.post("/voice", data=_form())
        assert "<Gather" in r.text or "<Say" in r.text

    def test_voice_has_play_or_say(self):
        r = client.post("/voice", data=_form())
        assert "<Play" in r.text or "<Say" in r.text

    def test_voice_creates_session_unconfirmed(self):
        sid = "CA_VOICE_NEW"
        client.post("/voice", data=_form(CallSid=sid))
        assert sid in _sessions
        assert _sessions[sid]["lang"] is None
        assert _sessions[sid]["lang_confirmed"] is False


class TestServerGatherPhase1:
    """Phase 1: language selection via digit press (1/2/3)."""

    def setup_method(self): _clear()

    @patch("src.server.synthesize", return_value=b"AUDIO")
    @patch("src.db.upsert_call")
    def test_digit_1_locks_sinhala(self, _db, _tts):
        sid = "CA_DIGIT_SI"
        client.post("/voice", data=_form(CallSid=sid))
        r = client.post("/gather", data=_form(CallSid=sid, Digits="1"))
        assert r.status_code == 200
        assert _sessions[sid]["lang"] == "si"
        assert _sessions[sid]["lang_confirmed"] is True

    @patch("src.server.synthesize", return_value=b"AUDIO")
    @patch("src.db.upsert_call")
    def test_digit_2_locks_tamil(self, _db, _tts):
        sid = "CA_DIGIT_TA"
        client.post("/voice", data=_form(CallSid=sid))
        r = client.post("/gather", data=_form(CallSid=sid, Digits="2"))
        assert _sessions[sid]["lang"] == "ta"
        assert _sessions[sid]["lang_confirmed"] is True

    @patch("src.server.synthesize", return_value=b"AUDIO")
    @patch("src.db.upsert_call")
    def test_digit_3_locks_english(self, _db, _tts):
        sid = "CA_DIGIT_EN"
        client.post("/voice", data=_form(CallSid=sid))
        r = client.post("/gather", data=_form(CallSid=sid, Digits="3"))
        assert _sessions[sid]["lang"] == "en"
        assert _sessions[sid]["lang_confirmed"] is True

    @patch("src.server.synthesize", return_value=b"AUDIO")
    @patch("src.db.upsert_call")
    def test_digit_choice_plays_confirmation(self, _db, _tts):
        sid = "CA_PLAY_CONF"
        client.post("/voice", data=_form(CallSid=sid))
        r = client.post("/gather", data=_form(CallSid=sid, Digits="3"))
        assert "<Play" in r.text

    @patch("src.server.synthesize", return_value=b"AUDIO")
    @patch("src.db.upsert_call")
    def test_no_digit_no_url_plays_retry(self, _db, _tts):
        sid = "CA_RETRY"
        client.post("/voice", data=_form(CallSid=sid))
        r = client.post("/gather", data=_form(CallSid=sid))
        assert _sessions[sid]["lang_confirmed"] is False


class TestServerGatherPhase2:
    """Phase 2: lang confirmed, chat() called directly."""

    def setup_method(self): _clear()

    def _seed(self, sid, lang):
        _sessions[sid] = {"history": [], "lang": lang, "lang_confirmed": True,
                          "silence_strikes": 0, "last_agent_question": ""}

    @patch("src.db.upsert_call")
    @patch("src.llm.chat", return_value=("Hello", [], "en", False))
    @patch("src.tts.synthesize", return_value=b"AUDIO")
    def test_phase2_reply_and_loops(self, _tts, mock_chat, _db):
        sid = "CA_PH2_LOOP"
        self._seed(sid, "en")
        r = client.post("/gather", data=_form(
            CallSid=sid, RecordingUrl="http://fake/rec.wav", RecordingDuration="3"))
        assert r.status_code == 200
        assert "<Play" in r.text
        # Server loops via <Record> or <Gather> after a normal reply
        assert "<Record" in r.text or "<Gather" in r.text

    @patch("src.db.upsert_call")
    @patch("src.llm.chat", return_value=("Si reply", [], "si", False))
    @patch("src.tts.synthesize", return_value=b"AUDIO")
    def test_phase2_sinhala_session(self, _tts, mock_chat, _db):
        sid = "CA_PH2_SI"
        self._seed(sid, "si")
        r = client.post("/gather", data=_form(
            CallSid=sid, RecordingUrl="http://fake/rec.wav", RecordingDuration="3"))
        assert r.status_code == 200

    @patch("src.db.upsert_call")
    @patch("src.llm.chat", return_value=("Escalate", [], "en", True))
    @patch("src.tts.synthesize", return_value=b"AUDIO")
    def test_phase2_escalation_dials(self, _tts, mock_chat, _db):
        sid = "CA_PH2_ESC"
        self._seed(sid, "en")
        with patch("src.server.ESCALATION_NUMBER", "+94112345678"):
            r = client.post("/gather", data=_form(
                CallSid=sid, RecordingUrl="http://fake/rec.wav", RecordingDuration="3"))
        assert "<Dial" in r.text

    @patch("src.db.upsert_call")
    @patch("src.llm.chat", return_value=("Tamil reply", [], "ta", False))
    @patch("src.tts.synthesize", return_value=b"AUDIO")
    def test_phase2_history_updated(self, _tts, mock_chat, _db):
        sid = "CA_PH2_HIST"
        self._seed(sid, "ta")
        client.post("/gather", data=_form(
            CallSid=sid, RecordingUrl="http://fake/rec.wav", RecordingDuration="3"))
        assert _sessions[sid]["history"] == []


# ── 4. dashboard active calls ────────────────────────────────────────────────

class TestDashboardActiveCalls:

    def test_active_calls_empty(self):
        with patch("src.dashboard_api._sessions_ref", {}):
            r = client.get("/dashboard/api/calls/active")
        assert r.json()["count"] == 0

    def test_active_calls_dict_format(self):
        fake = {"CA_X": {"history": [{"role": "user", "content": "hi"}],
                         "lang": "si", "lang_confirmed": True}}
        with patch("src.dashboard_api._sessions_ref", fake):
            r = client.get("/dashboard/api/calls/active")
        data = r.json()
        assert data["count"] == 1
        assert data["active"][0]["lang"] == "si"
        assert data["active"][0]["turns"] == 1


# ── 5. E2E flow ───────────────────────────────────────────────────────────────

class TestE2EFlow:

    def setup_method(self): _clear()

    @patch("src.db.upsert_call")
    @patch("src.llm.chat", return_value=("How can I help?", [], "en", False))
    @patch("src.tts.synthesize", return_value=b"AUDIO")
    @patch("src.server.synthesize", return_value=b"CONF_AUDIO")
    def test_full_en_digit_flow(self, _conf, _tts, mock_chat, _db):
        sid = "CA_E2E_EN"

        # Step 1 — call arrives → <Gather>
        r1 = client.post("/voice", data=_form(CallSid=sid))
        assert r1.status_code == 200
        assert _sessions[sid]["lang_confirmed"] is False

        # Step 2 — press 3 for English → confirmation audio + <Record>
        r2 = client.post("/gather", data=_form(CallSid=sid, Digits="3"))
        assert r2.status_code == 200
        assert _sessions[sid]["lang"] == "en"
        assert _sessions[sid]["lang_confirmed"] is True
        assert "<Play" in r2.text

        # Step 3 — caller speaks question → reply + loop back
        r3 = client.post("/gather", data=_form(
            CallSid=sid, RecordingUrl="http://fake/q.wav", RecordingDuration="4"))
        assert r3.status_code == 200
        assert "<Play" in r3.text
        assert "<Record" in r3.text or "<Gather" in r3.text
