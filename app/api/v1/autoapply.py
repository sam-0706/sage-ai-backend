"""Desktop auto-apply agent → SAGE LLM proxy → OpenRouter.

The desktop's AutA engine talks to this as an OpenAI-compatible provider whose "API key" is the user's SAGE session.
The OpenRouter key stays on the server, calls are metered per user (autoapply_calls), models are allow-listed,
and each call is logged to ai_runs with tokens and provider-reported cost.
"""
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.deps import Principal, get_principal
from app.core.config import get_settings
from app.core.context import get_correlation_id
from app.core.errors import AppError, FeatureDisabled, ProviderError
from app.core.logging import log
from app.integrations.http import client as http_client
from app.repositories import flags
from app.repositories.db import transaction
from app.services import entitlements

router = APIRouter(prefix="/autoapply", tags=["auto-apply"])
logger = logging.getLogger("sage.autoapply")
ALLOWED_KEYS = {"model", "messages", "temperature", "max_tokens", "response_format", "top_p", "stop"}


def _models() -> dict[str, dict]:
    s = get_settings()
    return {
        "sage-vision": {"upstream": s.autoapply_vision_model, "label": "SAGE vision planner", "vision": True},
        "sage-text": {"upstream": s.autoapply_text_model, "label": "SAGE fast text", "vision": False},
    }


@router.get("/status", summary="Auto-apply availability and remaining agent calls")
async def status(p: Principal = Depends(get_principal)):
    s = get_settings()
    async with transaction() as conn:
        enabled = await flags.is_enabled(conn, "auto_apply")
        sub = entitlements.summarize(await entitlements.active_subscription(conn, p.id))
    return {"enabled": enabled and bool(s.openrouter_api_key), "models": list(_models()),
            "quota": (sub or {}).get("autoapply_calls"), "plan": (sub or {}).get("plan_name")}


@router.get("/llm/models", summary="OpenAI-compatible model list for the desktop agent")
async def models(_: Principal = Depends(get_principal)):
    return {"object": "list", "data": [
        {"id": k, "object": "model", "name": v["label"],
         "architecture": {"input_modalities": ["text", "image"] if v["vision"] else ["text"]}} for k, v in _models().items()]}


@router.post("/llm/chat/completions", summary="OpenAI-compatible chat completions (metered, allow-listed)")
async def chat_completions(request: Request, p: Principal = Depends(get_principal)):
    s = get_settings()
    raw = await request.body()
    if len(raw) > s.autoapply_max_body_bytes:
        raise AppError("Request too large — send smaller screenshots", code="payload_too_large", status_code=413)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AppError("Invalid JSON body", code="invalid_json") from e
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise AppError("messages is required", code="invalid_request")
    if body.get("stream"):
        raise AppError("Streaming is not supported on this endpoint", code="stream_unsupported")
    if not s.openrouter_api_key:
        raise ProviderError("Auto-apply model access is not configured", code="autoapply_not_configured", status_code=503)

    models = _models()
    requested = str(body.get("model") or "sage-vision")
    alias = requested if requested in models else "sage-vision"
    payload = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    payload["model"] = models[alias]["upstream"]
    payload["max_tokens"] = min(int(payload.get("max_tokens") or 4096), 8192)
    payload["usage"] = {"include": True}

    async with transaction() as conn:
        if not await flags.is_enabled(conn, "auto_apply"):
            raise FeatureDisabled("Auto-apply is currently disabled")
        await entitlements.consume(conn, p.id, "autoapply_calls")

    started = time.perf_counter()
    try:
        resp = await http_client().post(
            f"{s.openrouter_base_url}/chat/completions", json=payload, timeout=httpx.Timeout(100.0, connect=10.0),
            headers={"Authorization": f"Bearer {s.openrouter_api_key}", "HTTP-Referer": s.public_base_url or "https://sage.ai",
                     "X-Title": "SAGE AI Desktop", "X-Request-ID": get_correlation_id()})
    except httpx.HTTPError as e:
        await _refund(p)
        raise ProviderError("Model provider is unavailable", code="autoapply_upstream_unavailable", status_code=503) from e

    latency = int((time.perf_counter() - started) * 1000)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"error": {"message": resp.text[:300]}}
    usage = data.get("usage") or {}
    await _log(p, alias, payload["model"], data.get("id"), latency, usage, resp.status_code)

    if resp.status_code >= 400:
        await _refund(p)
        # Pass provider 4xx through (the desktop engine adapts: e.g. retries without images/response_format).
        # Never pass through 401/403 — those concern OUR key, not the user's session.
        status_code = 502 if resp.status_code in (401, 403) or resp.status_code >= 500 else resp.status_code
        err = data.get("error") if isinstance(data.get("error"), dict) else {"message": str(data.get("error"))}
        raw_detail = (err.get("metadata") or {}).get("raw") if isinstance(err.get("metadata"), dict) else None
        msg = " — ".join(x for x in (err.get("message"), str(raw_detail)[:300] if raw_detail else None) if x)
        log(logger, logging.WARNING, "autoapply upstream error", status=resp.status_code, model=payload["model"])
        return JSONResponse({"error": {"code": "upstream_error", "message": (msg or "upstream error")[:500],
                                       "correlation_id": get_correlation_id()}}, status_code=status_code)
    data["model"] = alias  # hide upstream routing details from the client
    return data


async def _refund(p: Principal) -> None:
    async with transaction() as conn:
        await conn.execute("update subscriptions set autoapply_calls_used = greatest(0, autoapply_calls_used - 1) "
                           "where user_id = $1 and status = 'active'", p.id)


async def _log(p: Principal, alias: str, model: str, request_id, latency: int, usage: dict, status_code: int) -> None:
    cost = usage.get("cost")
    async with transaction() as conn:
        await conn.execute(
            """insert into ai_runs (user_id, task, prompt_version, model, provider_request_id, correlation_id, latency_ms,
                                    input_tokens, output_tokens, status, cost_usd)
               values ($1,'autoapply',$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            p.id, f"autoapply.{alias}", model, request_id, get_correlation_id(), latency, usage.get("prompt_tokens"),
            usage.get("completion_tokens"), "succeeded" if status_code < 400 else "failed",
            float(cost) if isinstance(cost, (int, float)) else None)
