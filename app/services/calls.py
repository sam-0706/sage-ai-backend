"""AI voice check-in lifecycle (PRD Epic 4).

requested → dispatching → dispatched → in_progress → completed | no_answer | busy | failed | cancelled

- Consent is recorded before dispatch; destination must be the user's own number or an allow-listed test number.
- Idempotency: (user_id, idempotency_key) is unique, and a partial unique index allows one live call per user.
- A provider "dispatched" response never marks a call complete; only a reconciled provider call log does.
"""
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, FeatureDisabled, Forbidden, NotFound, ProviderError, QuotaExceeded
from app.core.logging import log
from app.integrations import omnidim
from app.repositories import audit, flags
from app.repositories.db import row, rows, transaction
from app.services import entitlements

logger = logging.getLogger("sage.calls")

CONSENT_TEXT_V1 = ("I agree to receive an AI voice check-in call from SAGE AI at the number shown. The call is with an AI "
                   "coach, not a faculty member or advisor. It will be transcribed to create my plan. I can end the call at "
                   "any time. No one else will be contacted.")

CALL_COLS = """id, user_id, intervention_id, initiated_by, status::text as status, destination, consented_at,
               expected_duration_sec, estimated_minutes, provider, provider_request_id, provider_call_log_id, provider_status,
               provider_hangup_reason, duration_seconds, cost, transcript_status::text as transcript_status,
               extraction_status::text as extraction_status, is_simulated, error, reconcile_attempts, last_reconciled_at,
               dispatched_at, completed_at, created_at, updated_at"""
LIVE = ("requested", "dispatching", "dispatched", "in_progress")
RECONCILE_STALE_AFTER = timedelta(seconds=20)
GIVE_UP_AFTER = timedelta(minutes=30)


def normalize_number(n: str) -> str:
    digits = re.sub(r"[^\d+]", "", n or "")
    if not digits.startswith("+"):
        digits = "+" + digits
    if not re.fullmatch(r"\+\d{8,15}", digits):
        raise AppError("Phone number must be in international format, e.g. +919876543210", code="invalid_phone")
    return digits


def mask(n: str | None) -> str | None:
    return None if not n else n[:3] + "•" * max(0, len(n) - 7) + n[-4:]


def _allowed_destinations(user_phone: str | None) -> set[str]:
    s = get_settings()
    allowed = {normalize_number(n) for n in s.voice_allowed_test_numbers}
    if s.voice_allow_own_number and user_phone:
        try:
            allowed.add(normalize_number(user_phone))
        except AppError:
            pass
    return allowed


async def preflight(user: dict, intervention: dict | None) -> dict:
    """Everything the pre-call screen must show (PRD: purpose, number, duration, credit usage, consent)."""
    s = get_settings()
    async with transaction() as conn:
        enabled = await flags.is_enabled(conn, "voice_calls")
        remaining = await entitlements.remaining(conn, user["id"], "voice_seconds")
        live = row(await conn.fetchrow(f"select {CALL_COLS} from calls where user_id = $1 and status = any($2::call_status[])",
                                       user["id"], list(LIVE)))
    expected = s.voice_expected_duration_sec
    brief = (intervention or {}).get("brief", {}).get("intervention_brief", {})
    destinations = sorted(_allowed_destinations(user.get("phone")))
    return {
        "enabled": enabled and bool(s.omnidim_agent_id),
        "purpose": brief.get("call_purpose") or (intervention or {}).get("title"),
        "intervention_id": (intervention or {}).get("id"),
        "destinations": [{"number": d, "masked": mask(d), "is_own_number": d == (normalize_number(user["phone"]) if user.get("phone") else None)}
                         for d in destinations],
        "expected_duration_sec": expected,
        "estimated_minutes": round(expected / 60, 1),
        "remaining_voice_seconds": remaining,
        "may_exceed_allowance": expected > remaining,
        "consent_text": CONSENT_TEXT_V1,
        "consent_version": "v1",
        "live_call": live,
        "agent_disclosure": "You will speak with SAGE AI, an AI coach. It is not a faculty member or advisor and cannot make decisions for you.",
    }


def _call_context(user: dict, intervention: dict) -> dict:
    """Minimum necessary context for the voice agent — no payment data, no private notes, no transcripts."""
    brief = intervention.get("brief", {}).get("intervention_brief", {})
    first_name = (user.get("full_name") or "there").split(" ")[0]
    return {
        "first_name": first_name,
        "mode": user["mode"],
        "issue_title": intervention["title"],
        "issue_reason": intervention["reason"][:600],
        "evidence": "; ".join(e.get("fact", "") for e in intervention.get("evidence", []))[:800],
        "call_purpose": brief.get("call_purpose", intervention["title"])[:300],
        "opening_question": brief.get("opening_question", "")[:300],
        "key_questions": " | ".join(brief.get("key_questions", []))[:800],
        "hypotheses_to_explore": " | ".join(brief.get("hypotheses", []))[:600],
        "avoid": " | ".join(brief.get("avoid", []))[:400],
    }


async def create_call(user: dict, *, intervention_id: UUID, destination: str, consent: bool, consent_version: str,
                      idempotency_key: str, initiated_by: UUID) -> dict:
    s = get_settings()
    if not consent or consent_version != "v1":
        raise AppError("Explicit consent is required before a call can be placed", code="consent_required")
    dest = normalize_number(destination)
    if dest not in _allowed_destinations(user.get("phone")):
        raise Forbidden("Calls can only be placed to your own verified number or an approved test number",
                        code="destination_not_allowed")

    async with transaction() as conn:
        if not await flags.is_enabled(conn, "voice_calls"):
            raise FeatureDisabled("Voice check-ins are currently disabled")
        existing = row(await conn.fetchrow(f"select {CALL_COLS} from calls where user_id = $1 and idempotency_key = $2",
                                           user["id"], idempotency_key))
        if existing:
            return existing  # repeated tap / network retry → same call
        live = row(await conn.fetchrow(f"select {CALL_COLS} from calls where user_id = $1 and status = any($2::call_status[])",
                                       user["id"], list(LIVE)))
        if live:
            raise Conflict("A check-in call is already in progress", code="call_in_progress", details={"call_id": str(live["id"])})
        intervention = row(await conn.fetchrow(
            "select *, status::text as status_t from interventions where id = $1 and user_id = $2", intervention_id, user["id"]))
        if not intervention:
            raise NotFound("Intervention not found")
        if await entitlements.remaining(conn, user["id"], "voice_seconds") < 30:
            raise QuotaExceeded("Your plan has no voice minutes remaining", details={"meter": "voice_seconds"})
        call = row(await conn.fetchrow(
            f"""insert into calls (user_id, intervention_id, initiated_by, idempotency_key, status, destination, consent_text,
                                   consented_at, expected_duration_sec, estimated_minutes, provider_agent_id)
                values ($1,$2,$3,$4,'dispatching',$5,$6,now(),$7,$8,$9)
                on conflict (user_id, idempotency_key) do nothing returning {CALL_COLS}""",
            user["id"], intervention_id, initiated_by, idempotency_key, dest, CONSENT_TEXT_V1,
            s.voice_expected_duration_sec, round(s.voice_expected_duration_sec / 60, 2), s.omnidim_agent_id or None))
        if call is None:  # lost a race with an identical request
            return row(await conn.fetchrow(f"select {CALL_COLS} from calls where user_id = $1 and idempotency_key = $2",
                                           user["id"], idempotency_key))
        await conn.execute("update interventions set status = 'call_requested' where id = $1", intervention_id)
        await audit.record(conn, "call.consented", actor_id=initiated_by, target_type="call", target_id=call["id"],
                           metadata={"destination": mask(dest), "consent_version": consent_version})

    # Dispatch outside the DB transaction. Never auto-retried: a retry could ring the phone twice.
    try:
        request_id = await omnidim.dispatch_call(
            to_number=dest, call_context=_call_context(user, intervention),
            metadata={"sage_call_id": str(call["id"]), "sage_user_id": str(user["id"])})
    except ProviderError as e:
        definitive = e.code in ("voice_dispatch_rejected", "voice_not_configured") or e.status_code == 424
        async with transaction() as conn:
            if definitive:
                await conn.execute("update calls set status = 'failed', error = $2 where id = $1", call["id"], e.message)
                await conn.execute("update interventions set status = 'open' where id = $1", intervention_id)
            else:  # outcome unknown (timeout) → keep dispatching; reconciliation resolves it
                await conn.execute("update calls set error = $2 where id = $1", call["id"], "dispatch outcome uncertain; reconciling")
            await audit.record(conn, "call.dispatch_error", actor_id=initiated_by, target_type="call", target_id=call["id"],
                               metadata={"code": e.code, "definitive": definitive})
        if definitive:
            raise
        return await get_call(user["id"], call["id"], reconcile=False)

    async with transaction() as conn:
        updated = row(await conn.fetchrow(
            f"""update calls set status = 'dispatched', provider_request_id = $2, dispatched_at = now(), error = null
                where id = $1 returning {CALL_COLS}""", call["id"], request_id))
        await audit.record(conn, "call.dispatched", actor_id=initiated_by, target_type="call", target_id=call["id"],
                           metadata={"provider_request_id": request_id})
    return updated


async def get_call(user_id: UUID | None, call_id: UUID, *, reconcile: bool = True) -> dict:
    async with transaction() as conn:
        q = f"select {CALL_COLS} from calls where id = $1" + (" and user_id = $2" if user_id else "")
        call = row(await conn.fetchrow(q, *([call_id, user_id] if user_id else [call_id])))
    if not call:
        raise NotFound("Call not found")
    if reconcile and call["status"] in LIVE and not call["is_simulated"]:
        last = call["last_reconciled_at"] or call["dispatched_at"] or call["created_at"]
        if datetime.now(UTC) - last > RECONCILE_STALE_AFTER:
            try:
                call = await reconcile_call(call_id)
            except ProviderError as e:
                log(logger, logging.WARNING, "reconcile-on-read failed", call_id=str(call_id), err=e.code)
    if reconcile and call["status"] == "completed" and call["extraction_status"] == "pending":
        from app.services import postcall
        await postcall.run_extraction(call_id)
        return await get_call(user_id, call_id, reconcile=False)
    return call


async def list_calls(user_id: UUID, limit: int = 20) -> list[dict]:
    async with transaction() as conn:
        return rows(await conn.fetch(f"select {CALL_COLS} from calls where user_id = $1 order by created_at desc limit $2",
                                     user_id, limit))


async def reconcile_call(call_id: UUID) -> dict:
    """Pull verified state from the provider. Safe to run repeatedly (webhook, cron, poll)."""
    async with transaction() as conn:
        call = row(await conn.fetchrow(f"select {CALL_COLS} from calls where id = $1", call_id))
    if not call or call["status"] not in LIVE or call["is_simulated"]:
        return call

    found = None
    if call["provider_request_id"]:
        found = await omnidim.find_call_by_request_id(call["provider_request_id"])
    now = datetime.now(UTC)
    async with transaction() as conn:
        await conn.execute("update calls set reconcile_attempts = reconcile_attempts + 1, last_reconciled_at = now() where id = $1",
                           call_id)
        if not found:
            age = now - (call["dispatched_at"] or call["created_at"])
            if age > GIVE_UP_AFTER:
                # PRD edge case: provider said success but no final call exists → failed, never a fabricated transcript
                await conn.execute("update calls set status = 'failed', error = 'No provider call record found after reconciliation' "
                                   "where id = $1", call_id)
                await conn.execute("update interventions set status = 'open' where id = $1", call["intervention_id"])
                await audit.record(conn, "call.reconcile_gave_up", target_type="call", target_id=call_id)
            return row(await conn.fetchrow(f"select {CALL_COLS} from calls where id = $1", call_id))
        await _apply_provider_state(conn, call, found)
        return row(await conn.fetchrow(f"select {CALL_COLS} from calls where id = $1", call_id))


async def _apply_provider_state(conn, call: dict, pc: omnidim.ProviderCall) -> None:
    status = pc.status
    transcript_status, extraction_status = "none", "not_applicable"
    if status == "completed":
        if pc.transcript:
            words = len(pc.transcript.split())
            transcript_status = "available" if words >= 40 else "partial"
            extraction_status = "pending"
            await conn.execute(
                """insert into transcripts (call_id, raw_text, interactions, recording_url, provider_summary, provider_extracted, is_complete)
                   values ($1,$2,$3,$4,$5,$6,$7) on conflict (call_id) do update set raw_text = excluded.raw_text,
                     interactions = excluded.interactions, recording_url = excluded.recording_url,
                     provider_summary = excluded.provider_summary, provider_extracted = excluded.provider_extracted,
                     is_complete = excluded.is_complete""",
                call["id"], pc.transcript, pc.interactions, pc.recording_url, pc.summary, pc.extracted,
                transcript_status == "available")
        else:
            status = "completed"
    await conn.execute(
        """update calls set status = $2::call_status, provider_call_log_id = $3, provider_status = $4, provider_hangup_reason = $5,
                  duration_seconds = $6, cost = $7, transcript_status = $8::transcript_status,
                  extraction_status = case when extraction_status in ('succeeded','running') then extraction_status else $9::extraction_status end,
                  completed_at = case when $2 in ('completed','no_answer','busy','failed') then coalesce(completed_at, now()) else completed_at end,
                  error = null
           where id = $1""",
        call["id"], status, pc.call_log_id, pc.raw_status, pc.hangup_reason, pc.duration_seconds, pc.cost,
        transcript_status, extraction_status)
    if status in omnidim.FINAL_STATUSES:
        seconds = int(round(pc.duration_seconds))
        if seconds > 0 and call["status"] != "completed":
            await entitlements.consume(conn, call["user_id"], "voice_seconds", seconds, allow_overdraw=True)
        new_iv = "in_call" if status == "in_progress" else ("awaiting_review" if extraction_status == "pending" else "open")
        await conn.execute("update interventions set status = $2::intervention_status where id = $1",
                           call["intervention_id"], new_iv)
        await audit.record(conn, "call.final_state", target_type="call", target_id=call["id"],
                           metadata={"status": status, "duration": pc.duration_seconds, "transcript": transcript_status})
    elif status == "in_progress":
        await conn.execute("update interventions set status = 'in_call' where id = $1", call["intervention_id"])


async def cancel_call(user_id: UUID, call_id: UUID) -> dict:
    """Stop path: cancel a call that has not connected yet. A connected call is ended by hanging up."""
    async with transaction() as conn:
        call = row(await conn.fetchrow(f"select {CALL_COLS} from calls where id = $1 and user_id = $2 for update", call_id, user_id))
        if not call:
            raise NotFound("Call not found")
        if call["status"] not in ("requested", "dispatching", "dispatched"):
            raise Conflict("This call can no longer be cancelled from the app; hang up to end it", code="call_not_cancellable")
        await conn.execute("update calls set status = 'cancelled', completed_at = now() where id = $1", call_id)
        await conn.execute("update interventions set status = 'open' where id = $1", call["intervention_id"])
        await audit.record(conn, "call.cancelled", actor_id=user_id, target_type="call", target_id=call_id)
        return row(await conn.fetchrow(f"select {CALL_COLS} from calls where id = $1", call_id))


async def reconcile_open_calls(limit: int = 25) -> dict:
    async with transaction() as conn:
        ids = [r["id"] for r in await conn.fetch(
            "select id from calls where status in ('dispatching','dispatched','in_progress') and is_simulated = false "
            "order by coalesce(last_reconciled_at, created_at) limit $1", limit)]
        pending_extraction = [r["id"] for r in await conn.fetch(
            "select id from calls where status = 'completed' and extraction_status = 'pending' limit $1", limit)]
    results = {"reconciled": 0, "errors": 0, "extracted": 0}
    for cid in ids:
        try:
            await reconcile_call(cid)
            results["reconciled"] += 1
        except ProviderError:
            results["errors"] += 1
    from app.services import postcall
    for cid in pending_extraction:
        try:
            await postcall.run_extraction(cid)
            results["extracted"] += 1
        except ProviderError:
            results["errors"] += 1
    return results
