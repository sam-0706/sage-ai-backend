"""Unit & contract tests that need no network or database."""
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.errors import AppError, Unauthorized
from app.domain.ai_schemas import ExtractionOutput, PriorityOutput
from app.integrations import clerk_api, omnidim, razorpay
from app.services.calls import mask, normalize_number
from app.services.knowledge import chunk_text, html_to_text
from app.services.priority import detect_events


def sig(**kw):
    base = dict(id="s1", type="attendance", label="Stats attendance", subject="Stats", value_num=62, value_text=None,
                threshold=75, unit="%", due_at=None, source="demo")
    return base | kw


# ---------------------------------------------------------------- rules engine
def test_attendance_below_threshold_detected():
    ev = detect_events([sig()])
    assert ev and ev[0]["type"] == "attendance" and ev[0]["rule_score"] > 55


def test_attendance_above_threshold_ignored():
    assert detect_events([sig(value_num=88)]) == []


def test_near_deadline_ranks_above_far_deadline():
    now = datetime.now(UTC)
    ev = detect_events([sig(id="a", type="deadline", value_num=None, threshold=None, due_at=now + timedelta(days=8)),
                        sig(id="b", type="deadline", value_num=None, threshold=None, due_at=now + timedelta(days=1))])
    assert [e["signal_id"] for e in ev] == ["b", "a"]


def test_overdue_fee_is_top_priority():
    ev = detect_events([sig(), sig(id="f", type="fee", value_num=None, threshold=None, due_at=datetime.now(UTC) - timedelta(days=1))])
    assert ev[0]["signal_id"] == "f"


# ---------------------------------------------------------------- AI output contracts are strict-schema compatible
@pytest.mark.parametrize("model", [PriorityOutput, ExtractionOutput])
def test_ai_schemas_have_all_fields_required(model):
    schema = model.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])


def test_extraction_contains_prd_minimum_fields():
    fields = set(ExtractionOutput.model_fields)
    assert {"issue", "evidence", "root_cause", "actions", "owner", "due_date", "risk", "confidence", "escalation"} <= fields


# ---------------------------------------------------------------- Clerk token verification
@pytest.fixture
def rsa_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJWK:
        def __init__(self, k):
            self.key = k

    class FakeClient:
        def get_signing_key_from_jwt(self, _):
            return FakeJWK(key.public_key())

    monkeypatch.setattr(clerk_api, "_jwks_client", lambda: FakeClient())
    monkeypatch.setattr(clerk_api, "_issuer_and_jwks", lambda: ("https://issuer.test", "https://issuer.test/jwks"))
    return key


def make_token(key, **claims):
    now = int(time.time())
    return jwt.encode({"sub": "user_1", "iss": "https://issuer.test", "iat": now, "exp": now + 60} | claims, key, algorithm="RS256")


def test_valid_token(rsa_key):
    assert clerk_api.verify_session_token(make_token(rsa_key))["sub"] == "user_1"


def test_expired_token(rsa_key):
    with pytest.raises(Unauthorized) as e:
        clerk_api.verify_session_token(make_token(rsa_key, exp=int(time.time()) - 120))
    assert e.value.code == "token_expired"


def test_wrong_issuer(rsa_key):
    with pytest.raises(Unauthorized):
        clerk_api.verify_session_token(make_token(rsa_key, iss="https://evil.test"))


def test_tampered_token(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Unauthorized):
        clerk_api.verify_session_token(make_token(other))


def test_publishable_key_decoding():
    import base64
    pk = "pk_test_" + base64.b64encode(b"exact-mustang-4776.clerk.accounts.dev$").decode().rstrip("=")
    assert clerk_api._frontend_api_from_publishable_key(pk) == "exact-mustang-4776.clerk.accounts.dev"


# ---------------------------------------------------------------- payments
def test_razorpay_signature_verification():
    good = hmac.new(b"secret123", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert razorpay.verify_payment_signature("order_1", "pay_1", good)
    assert not razorpay.verify_payment_signature("order_1", "pay_2", good)
    assert not razorpay.verify_payment_signature("order_1", "pay_1", "")


def test_razorpay_webhook_signature():
    body = b'{"event":"payment.captured"}'
    good = hmac.new(b"whsecret", body, hashlib.sha256).hexdigest()
    assert razorpay.verify_webhook_signature(body, good)
    assert not razorpay.verify_webhook_signature(body + b" ", good)


def test_live_razorpay_keys_refused(monkeypatch):
    from app.core import config
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abc")
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        config.get_settings()
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    config.get_settings.cache_clear()


def test_dev_bypass_refused_outside_development(monkeypatch):
    from app.core import config
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("APP_ENV", "production")
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        config.get_settings()
    monkeypatch.setenv("DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("APP_ENV", "test")
    config.get_settings.cache_clear()


# ---------------------------------------------------------------- voice
def test_phone_normalization():
    assert normalize_number("+91 90598 32002") == "+919059832002"
    assert normalize_number("919059832002") == "+919059832002"
    with pytest.raises(AppError):
        normalize_number("12")
    assert mask("+919059832002").endswith("2002") and "•" in mask("+919059832002")


@pytest.mark.parametrize("raw,expected", [("completed", "completed"), ("no-answer", "no_answer"), ("busy", "busy"),
                                          ("failed", "failed"), ("ringing", "in_progress"), (None, "in_progress")])
def test_provider_status_normalization(raw, expected):
    assert omnidim.normalize_status(raw) == expected


def test_transcript_cleaning():
    assert omnidim.clean_transcript("AI: hi<br/>User: hello<br>") == "AI: hi\nUser: hello"


# ---------------------------------------------------------------- knowledge
def test_chunking_keeps_headings_and_size():
    text = "# Title\n\n## Attendance\n\n" + ("Students must attend. " * 200) + "\n\n## Fees\n\nPay by the 10th."
    chunks = chunk_text(text)
    assert all(len(c) <= 1800 for _, c in chunks)
    assert any(h == "Fees" for h, _ in chunks) and any(h == "Attendance" for h, _ in chunks)


def test_html_to_text_strips_scripts():
    out = html_to_text("<html><script>alert(1)</script><h2>Policy</h2><p>Attend 75%.</p></html>")
    assert "alert" not in out and "## Policy" in out and "Attend 75%." in out
