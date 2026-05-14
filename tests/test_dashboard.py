import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from src.server import app

client = TestClient(app)

@pytest.fixture
def mock_chat():
    with patch("src.dashboard_api.chat") as mock:
        yield mock

@pytest.fixture
def mock_synthesize():
    with patch("src.dashboard_api.synthesize") as mock:
        mock.return_value = b"fake audio bytes"
        yield mock

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_dashboard_static_files():
    # Test index.html
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]
    
    # Test CSS
    response = client.get("/dashboard/css/dashboard.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]

def test_api_test_text(mock_chat, mock_synthesize):
    # Setup mock
    mock_chat.return_value = ("Test reply", [], "en", False)
    
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": "Hello", "history": []}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == "Test reply"
    assert data["lang"] == "en"
    assert "audio_b64" in data
    
    mock_chat.assert_called_once_with("Hello", [], locked_lang=None)
    mock_synthesize.assert_called_once_with("Test reply", "en")

def test_api_test_text_empty_message():
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": "", "history": []}
    )
    assert response.status_code == 400

def test_api_get_settings():
    response = client.get("/dashboard/api/settings")
    assert response.status_code == 200
    data = response.json()
    assert "env" in data
    assert "llm_params" in data
    assert "max_tokens" in data["llm_params"]
    assert "temperature" in data["llm_params"]

def test_api_get_stats():
    response = client.get("/dashboard/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_calls" in data
    assert "escalated" in data

def test_api_list_calls():
    response = client.get("/dashboard/api/calls")
    assert response.status_code == 200
    data = response.json()
    assert "calls" in data
    assert isinstance(data["calls"], list)

@patch("src.dashboard_api._sessions_ref", {"fake_sid": []})
def test_api_active_calls():
    response = client.get("/dashboard/api/calls/active")
    assert response.status_code == 200
    data = response.json()
    assert "active" in data
    assert len(data["active"]) == 1
    assert data["active"][0]["call_sid"] == "fake_sid"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 direct lang_code tests (Step 2 of overhaul plan)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_synthesize_lang():
    with patch("src.dashboard_api.synthesize") as mock:
        mock.return_value = b"fake audio bytes"
        yield mock


@pytest.mark.parametrize("lang_code,expected_lang", [
    ("si", "si"),
    ("en", "en"),
    ("ta", "ta"),
])
def test_phase1_direct_code(mock_synthesize_lang, lang_code, expected_lang):
    """POST {lang_phase:1, lang_code:'si'/'en'/'ta'} → chosen_lang set correctly."""
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": lang_code, "history": [], "lang_phase": 1, "lang_code": lang_code},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["chosen_lang"] == expected_lang, \
        f"lang_code='{lang_code}' should yield chosen_lang='{expected_lang}', got '{data['chosen_lang']}'"
    assert data["phase"] == 1


def test_phase1_direct_code_si(mock_synthesize_lang):
    """test_phase1_direct_code_si — named alias for spec compliance."""
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": "si", "history": [], "lang_phase": 1, "lang_code": "si"},
    )
    assert response.json()["chosen_lang"] == "si"


def test_phase1_direct_code_en(mock_synthesize_lang):
    """test_phase1_direct_code_en — named alias for spec compliance."""
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": "en", "history": [], "lang_phase": 1, "lang_code": "en"},
    )
    assert response.json()["chosen_lang"] == "en"


def test_phase1_direct_code_ta(mock_synthesize_lang):
    """test_phase1_direct_code_ta — named alias for spec compliance."""
    response = client.post(
        "/dashboard/api/test-text",
        json={"message": "ta", "history": [], "lang_phase": 1, "lang_code": "ta"},
    )
    assert response.json()["chosen_lang"] == "ta"


# ─────────────────────────────────────────────────────────────────────────────
# Rebrand tests (Step 1 of overhaul plan)
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_name_is_not_nimali():
    """Greeting text must not contain 'Nimali'."""
    from src.language import LANG_SELECTION_GREETING, OPENING_GREETING
    assert "Nimali" not in LANG_SELECTION_GREETING, "LANG_SELECTION_GREETING contains 'Nimali'"
    assert "Nimali" not in OPENING_GREETING, "OPENING_GREETING contains 'Nimali'"


def test_confirmation_text_is_not_nimali():
    """All 3 lang confirmations must not contain 'Nimali'."""
    from src.language import LANG_CONFIRMATIONS
    for code, text in LANG_CONFIRMATIONS.items():
        assert "Nimali" not in text, f"LANG_CONFIRMATIONS['{code}'] contains 'Nimali'"
