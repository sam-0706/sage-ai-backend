"""OmniDimension voice adapter — the only module that knows OmniDimension's API shape.

Dispatch is NOT retried automatically (a retry could place a second real phone call). Final state always comes
from the provider's call log (matched by `call_request_id`), never from the dispatch response alone.
"""
import re
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.integrations.http import request_json

FINAL_STATUSES = {"completed", "no_answer", "busy", "failed"}


@dataclass
class ProviderCall:
    call_log_id: str
    status: str  # normalized: in_progress | completed | no_answer | busy | failed
    raw_status: str
    duration_seconds: float
    transcript: str
    interactions: list
    recording_url: str | None
    summary: str | None
    extracted: dict
    hangup_reason: str | None
    cost: dict
    to_number: str | None


def _headers() -> dict:
    s = get_settings()
    if not s.omnidim_api_key:
        raise ProviderError("Voice provider is not configured", code="voice_not_configured", status_code=503)
    return {"Authorization": f"Bearer {s.omnidim_api_key}", "Content-Type": "application/json"}


def normalize_status(raw: str | None) -> str:
    r = (raw or "").lower().replace("-", "_").replace(" ", "_")
    if r in ("completed", "complete", "ended", "success"):
        return "completed"
    if r in ("no_answer", "noanswer", "not_answered", "unanswered", "voicemail", "voicemail_detected"):
        return "no_answer"
    if r == "busy":
        return "busy"
    if r in ("failed", "error", "canceled", "cancelled", "rejected"):
        return "failed"
    return "in_progress"


def clean_transcript(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


async def dispatch_call(*, to_number: str, call_context: dict, metadata: dict) -> str:
    s = get_settings()
    if not s.omnidim_agent_id:
        raise ProviderError("Voice agent is not configured", code="voice_not_configured", status_code=503)
    body = {"agent_id": int(s.omnidim_agent_id), "to_number": to_number, "call_context": call_context, "metadata": metadata}
    if s.omnidim_from_number_id:
        body["from_number_id"] = int(s.omnidim_from_number_id)
    data = await request_json("POST", f"{s.omnidim_base_url}/calls/dispatch", headers=_headers(), json=body,
                              provider="omnidim", timeout=25.0, retries=0)
    if not isinstance(data, dict) or not data.get("success") or not data.get("requestId"):
        raise ProviderError("Voice provider did not accept the call", code="voice_dispatch_rejected")
    return str(data["requestId"])


def _to_provider_call(log: dict) -> ProviderCall:
    return ProviderCall(
        call_log_id=str(log.get("id")),
        status=normalize_status(log.get("call_status")),
        raw_status=str(log.get("call_status")),
        duration_seconds=float(log.get("call_duration_in_seconds") or 0),
        transcript=clean_transcript(log.get("call_conversation")),
        interactions=log.get("interactions") or [],
        recording_url=log.get("recording_url") or None,
        summary=(log.get("call_report") or {}).get("summary") if isinstance(log.get("call_report"), dict) else None,
        extracted=log.get("extracted_variables") or {},
        hangup_reason=log.get("hangup_reason") or None,
        cost={k: log.get(k) for k in ("call_cost", "voiceai_cost", "telephony_cost", "aggregated_estimated_cost", "total_tokens")
              if log.get(k) is not None},
        to_number=log.get("to_number"),
    )


async def find_call_by_request_id(request_id: str, max_pages: int = 3) -> ProviderCall | None:
    """Reconcile: locate the call log whose call_request_id matches our dispatch requestId."""
    s = get_settings()
    for page in range(1, max_pages + 1):
        data = await request_json("GET", f"{s.omnidim_base_url}/calls/logs", headers=_headers(), provider="omnidim",
                                  params={"pageno": page, "pagesize": 50, "agentid": s.omnidim_agent_id})
        logs = data.get("call_log_data", []) if isinstance(data, dict) else []
        for log in logs:
            req = log.get("call_request_id")
            rid = req.get("id") if isinstance(req, dict) else req
            if rid is not None and str(rid) == str(request_id):
                return _to_provider_call(log)
        if len(logs) < 50:
            break
    return None


async def get_call_log(call_log_id: str) -> ProviderCall | None:
    s = get_settings()
    data = await request_json("GET", f"{s.omnidim_base_url}/calls/logs/{call_log_id}", headers=_headers(), provider="omnidim")
    logs = data.get("call_log_data", []) if isinstance(data, dict) else []
    return _to_provider_call(logs[0]) if logs else None


async def create_agent(payload: dict) -> dict:
    s = get_settings()
    return await request_json("POST", f"{s.omnidim_base_url}/agents/create", headers=_headers(), json=payload,
                              provider="omnidim", retries=0)


async def update_agent(agent_id: str, payload: dict) -> dict:
    s = get_settings()
    return await request_json("PUT", f"{s.omnidim_base_url}/agents/{agent_id}", headers=_headers(), json=payload,
                              provider="omnidim")
