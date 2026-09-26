"""Provider webhooks: validated, stored idempotently, then followed by reconciliation (PRD: Reliability)."""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, Query, Request

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.logging import log
from app.integrations.razorpay import verify_webhook_signature
from app.repositories.db import transaction
from app.services import billing
from app.services import calls as calls_svc

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("sage.webhooks")


async def _store(provider: str, dedupe_key: str, payload: dict) -> bool:
    """Returns False when this exact event was already processed."""
    async with transaction() as conn:
        inserted = await conn.fetchval(
            """insert into webhook_events (provider, dedupe_key, payload) values ($1,$2,$3)
               on conflict (provider, dedupe_key) do nothing returning id""", provider, dedupe_key, payload)
        if inserted:
            return True
        return await conn.fetchval("select status from webhook_events where provider = $1 and dedupe_key = $2",
                                   provider, dedupe_key) == "failed"


async def _mark(provider: str, dedupe_key: str, status: str, error: str | None = None) -> None:
    async with transaction() as conn:
        await conn.execute("update webhook_events set status = $3, error = $4, processed_at = now() "
                           "where provider = $1 and dedupe_key = $2", provider, dedupe_key, status, error)


@router.post("/omnidim", summary="OmniDimension post-call webhook (secret token in query string)")
async def omnidim_webhook(request: Request, token: str = Query("")):
    secret = get_settings().omnidim_webhook_secret
    if not secret or not hmac.compare_digest(token, secret):
        raise Unauthorized("Invalid webhook token")
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {"raw": body.decode(errors="ignore")[:5000]}
    dedupe = hashlib.sha256(body).hexdigest()
    if not await _store("omnidim", dedupe, payload):
        return {"status": "duplicate"}

    # The payload is a hint only: we locate our call, then trust the provider API (reconciliation), not the body.
    meta = payload.get("metadata") or (payload.get("call_report") or {}).get("metadata") or {}
    sage_call_id = meta.get("sage_call_id") if isinstance(meta, dict) else None
    req_id = payload.get("call_request_id") or payload.get("requestId") or payload.get("request_id")
    if isinstance(req_id, dict):
        req_id = req_id.get("id")
    async with transaction() as conn:
        call_id = None
        if sage_call_id:
            call_id = await conn.fetchval("select id from calls where id::text = $1", str(sage_call_id))
        if not call_id and req_id:
            call_id = await conn.fetchval("select id from calls where provider_request_id = $1", str(req_id))
        if not call_id:  # fall back: newest live call to the same number
            to = payload.get("to_number") or payload.get("phone_number")
            if to:
                call_id = await conn.fetchval(
                    "select id from calls where destination = $1 and status in ('dispatched','in_progress') order by created_at desc limit 1",
                    calls_svc.normalize_number(str(to)))
    if not call_id:
        await _mark("omnidim", dedupe, "ignored", "no matching call")
        return {"status": "ignored"}
    try:
        call = await calls_svc.reconcile_call(call_id)
        if call and call["status"] == "completed" and call["extraction_status"] == "pending":
            from app.services import postcall
            await postcall.run_extraction(call_id)
        await _mark("omnidim", dedupe, "processed")
    except Exception as e:  # noqa: BLE001 — recorded; cron reconciliation retries
        log(logger, logging.WARNING, "omnidim webhook processing failed", err=type(e).__name__)
        await _mark("omnidim", dedupe, "failed", type(e).__name__)
    return {"status": "accepted"}


@router.post("/razorpay", summary="Razorpay webhook (X-Razorpay-Signature verified)")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(""), x_razorpay_event_id: str = Header("")):
    body = await request.body()
    if not verify_webhook_signature(body, x_razorpay_signature):
        raise Unauthorized("Invalid webhook signature")
    payload = json.loads(body)
    dedupe = x_razorpay_event_id or hashlib.sha256(body).hexdigest()
    if not await _store("razorpay", dedupe, payload):
        return {"status": "duplicate"}
    try:
        result = await billing.handle_webhook(payload)
        await _mark("razorpay", dedupe, result)
    except Exception as e:  # noqa: BLE001
        await _mark("razorpay", dedupe, "failed", type(e).__name__)
        raise
    return {"status": result}
