"""Razorpay adapter (test mode only). Orders are created server-side; signatures are verified server-side."""
import hashlib
import hmac

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.integrations.http import request_json

API = "https://api.razorpay.com/v1"


def _auth() -> tuple[str, str]:
    s = get_settings()
    if not (s.razorpay_key_id and s.razorpay_key_secret):
        raise ProviderError("Payments are not configured", code="payments_not_configured", status_code=503)
    return s.razorpay_key_id, s.razorpay_key_secret


async def create_order(*, amount: int, currency: str, receipt: str, notes: dict) -> dict:
    # Not retried automatically; our payment_orders row (unique per idempotency key) prevents duplicates.
    return await request_json("POST", f"{API}/orders", auth=_auth(), provider="razorpay", retries=0,
                              json={"amount": amount, "currency": currency, "receipt": receipt[:40], "notes": notes})


async def fetch_payment(payment_id: str) -> dict:
    return await request_json("GET", f"{API}/payments/{payment_id}", auth=_auth(), provider="razorpay")


async def fetch_order(order_id: str) -> dict:
    return await request_json("GET", f"{API}/orders/{order_id}", auth=_auth(), provider="razorpay")


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    _, secret = _auth()
    expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = get_settings().razorpay_webhook_secret
    if not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
