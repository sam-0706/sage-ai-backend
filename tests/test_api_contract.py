"""API contract tests via ASGI (no DB): auth required everywhere, consistent error envelope, OpenAPI present."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_openapi_lists_versioned_routes():
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/v1/me", "/v1/home", "/v1/interventions/prioritize", "/v1/calls", "/v1/plans/{plan_id}/share",
              "/v1/chat/sessions/{session_id}/messages", "/v1/billing/orders", "/v1/staff/cases", "/v1/admin/users"):
        assert p in paths, p


def test_protected_routes_require_bearer_token():
    for method, path in (("get", "/v1/me"), ("post", "/v1/interventions/prioritize"), ("get", "/v1/admin/overview"),
                         ("get", "/v1/staff/cases"), ("get", "/v1/billing/plans")):
        r = getattr(client, method)(path)
        assert r.status_code == 401
        body = r.json()["error"]
        assert body["code"] == "unauthorized" and body["correlation_id"]


def test_error_envelope_for_unknown_route():
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_webhook_rejects_bad_token():
    assert client.post("/v1/webhooks/omnidim?token=wrong", json={}).status_code == 401


def test_razorpay_webhook_rejects_bad_signature():
    assert client.post("/v1/webhooks/razorpay", content=b"{}", headers={"X-Razorpay-Signature": "x"}).status_code == 401


def test_cron_requires_secret():
    assert client.get("/v1/internal/cron/maintenance").status_code == 401


def test_correlation_id_echoed():
    r = client.get("/health/live", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
