"""Exam prep scheduling, device sign-in helpers and new route contracts (no DB / network)."""
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.domain.ai_schemas import DeckOutput, ExamAssessmentOutput
from app.main import app
from app.services import device_auth
from app.services.examprep import schedule

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def card(**kw):
    return {"ease": 2.5, "interval_days": 0, "reps": 0, "lapses": 0} | kw


def test_again_resets_and_counts_lapse():
    nxt = schedule(card(reps=3, interval_days=10), 1, NOW)
    assert nxt["reps"] == 0 and nxt["lapses"] == 1 and nxt["due_at"] == NOW + timedelta(minutes=10)
    assert nxt["ease"] == 2.3


def test_good_progression_1_3_then_multiplied():
    first = schedule(card(), 3, NOW)
    second = schedule(card(**first), 3, NOW)
    third = schedule(card(**second), 3, NOW)
    assert (first["interval_days"], second["interval_days"]) == (1.0, 3.0)
    assert third["interval_days"] == pytest.approx(7.5)


def test_easy_grows_faster_than_good_and_raises_ease():
    good, easy = schedule(card(reps=2, interval_days=3), 3, NOW), schedule(card(reps=2, interval_days=3), 4, NOW)
    assert easy["interval_days"] > good["interval_days"] and easy["ease"] > good["ease"]


def test_ease_never_below_floor():
    c = card(ease=1.3)
    for _ in range(5):
        c = card(**schedule(c, 2, NOW))
    assert c["ease"] >= 1.3


def test_interval_capped_at_one_year():
    assert schedule(card(reps=10, interval_days=300, ease=3.0), 4, NOW)["interval_days"] <= 365


def test_user_codes_are_readable_and_unambiguous():
    for _ in range(200):
        code = device_auth._user_code()
        assert re.fullmatch(r"[BCDFGHJKLMNPQRSTVWXZ2-9]{4}-[BCDFGHJKLMNPQRSTVWXZ2-9]{4}", code)
        assert not set(code) & set("AEIOU01")


def test_device_code_hash_is_not_reversible_plaintext():
    h = device_auth._hash("secret-device-code")
    assert h != "secret-device-code" and len(h) == 64


@pytest.mark.parametrize("model", [DeckOutput, ExamAssessmentOutput])
def test_exam_schemas_strict_compatible(model):
    schema = model.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])


client = TestClient(app)


def test_new_routes_require_auth():
    for method, path in (("get", "/v1/onboarding"), ("get", "/v1/exam-prep/decks"), ("post", "/v1/exam-prep/decks"),
                         ("get", "/v1/autoapply/status"), ("post", "/v1/autoapply/llm/chat/completions"),
                         ("post", "/v1/auth/device/approve"), ("get", "/v1/auth/sessions")):
        assert getattr(client, method)(path).status_code in (401, 422), path


def test_session_token_prefix_is_rejected_when_unknown(monkeypatch):
    async def boom(token):
        from app.core.errors import Unauthorized
        raise Unauthorized("Session expired or revoked — please sign in again", code="session_invalid")
    monkeypatch.setattr(device_auth, "user_for_session_token", boom)
    r = client.get("/v1/me", headers={"Authorization": "Bearer sds_not-a-real-token"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_invalid"


def test_device_page_escapes_code():
    r = client.get("/auth/device?code=<script>alert(1)</script>")
    assert r.status_code == 200 and "<script>alert" not in r.text.split("const CODE")[0]
    assert "Open SAGE" in r.text and "mountSignIn" not in r.text


def test_browser_ticket_is_authenticated_and_expires(monkeypatch):
    from app.core.errors import Unauthorized
    monkeypatch.setattr(device_auth.time, "time", lambda: 1000)
    ticket = device_auth.browser_ticket("BCDF-GHJK")
    assert device_auth.verify_browser_ticket(ticket) == "BCDF-GHJK"
    with pytest.raises(Unauthorized):
        device_auth.verify_browser_ticket(ticket.replace("BCDF", "XXXX"))
    monkeypatch.setattr(device_auth.time, "time", lambda: 1601)
    with pytest.raises(Unauthorized):
        device_auth.verify_browser_ticket(ticket)


def test_google_page_requires_valid_ticket_and_has_no_hosted_form(monkeypatch):
    assert client.get("/auth/google?ticket=invalid").status_code == 401
    async def pending(code):
        return {"status": "pending"}
    monkeypatch.setattr(device_auth, "describe", pending)
    ticket = device_auth.browser_ticket("BCDF-GHJK")
    response = client.get("/auth/google", params={"ticket": ticket})
    assert response.status_code == 200
    assert "oauth_google" in response.text and "handleRedirectCallback" in response.text
    assert "mountSignIn" not in response.text and 'class="code"' not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.post("/v1/auth/google/complete", json={"ticket": ticket}).status_code == 401


def test_google_page_rejects_used_requests(monkeypatch):
    async def consumed(code):
        return {"status": "consumed"}
    monkeypatch.setattr(device_auth, "describe", consumed)
    assert client.get("/auth/google", params={"ticket": device_auth.browser_ticket("BCDF-GHJK")}).status_code == 410
