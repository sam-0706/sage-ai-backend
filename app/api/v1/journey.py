"""Core journey: profile & signals → priority → consented call → extraction → editable plan → optional share."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth.deps import Principal, get_principal
from app.core.errors import AppError, NotFound
from app.domain.schemas import (CallCreate, LoadDemoIn, PlanCreate, PlanPatch, ProfileIn, ShareIn, SignalIn, SignalPatch,
                                SimulateCallIn)
from app.repositories import users as users_repo
from app.repositories.db import rows, transaction
from app.services import calls as calls_svc
from app.services import plans as plans_svc
from app.services import postcall, priority
from app.services import profile as profile_svc

router = APIRouter()


# ------------------------------------------------------------------ profile & signals
@router.get("/profile", tags=["profile"], summary="Goal profile with per-field provenance")
async def get_profile(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await profile_svc.get_profile(conn, p.id, p.mode)


@router.put("/profile", tags=["profile"], summary="Create or update the goal profile for a mode")
async def put_profile(body: ProfileIn, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await profile_svc.upsert_profile(conn, p.id, body.mode, body.data, body.availability, body.consent)


@router.get("/demo-profiles", tags=["profile"], summary="Labelled synthetic demo profiles")
async def demo_profiles(mode: str | None = None, _: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await profile_svc.list_demo_profiles(conn, mode)}


@router.post("/profile/load-demo", tags=["profile"], summary="Replace own profile & signals with a synthetic demo profile")
async def load_demo(body: LoadDemoIn, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        prof = await profile_svc.load_demo_profile(conn, p.id, body.key)
        return {"profile": prof, "signals": await profile_svc.list_signals(conn, p.id)}


@router.get("/signals", tags=["profile"])
async def list_signals(include_inactive: bool = False, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await profile_svc.list_signals(conn, p.id, include_inactive)}


@router.post("/signals", tags=["profile"], status_code=201)
async def create_signal(body: SignalIn, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await profile_svc.create_signal(conn, p.id, body.model_dump())


@router.patch("/signals/{signal_id}", tags=["profile"], summary="Correct, dispute or dismiss a signal")
async def patch_signal(signal_id: UUID, body: SignalPatch, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await profile_svc.update_signal(conn, p.id, signal_id, body.model_dump(exclude_unset=True))


# ------------------------------------------------------------------ priorities
@router.post("/interventions/prioritize", tags=["interventions"],
             summary="Detect signals, rank with AI, and create an intervention brief (falls back to rules)")
async def prioritize(p: Principal = Depends(get_principal)):
    return await priority.prioritize(p.id, p.mode, p.full_name.split(" ")[0] if p.full_name else None)


@router.get("/interventions", tags=["interventions"])
async def list_interventions(limit: int = Query(20, le=100), p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await priority.list_interventions(conn, p.id, limit)}


@router.get("/interventions/{intervention_id}", tags=["interventions"])
async def get_intervention(intervention_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        iv = await priority.get_intervention(conn, p.id, intervention_id)
    if not iv:
        raise NotFound("Intervention not found")
    return iv


@router.post("/interventions/{intervention_id}/dismiss", tags=["interventions"])
async def dismiss_intervention(intervention_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        n = await conn.execute("update interventions set status = 'dismissed' where id = $1 and user_id = $2", intervention_id, p.id)
    if n.endswith(" 0"):
        raise NotFound("Intervention not found")
    return {"dismissed": True}


# ------------------------------------------------------------------ voice check-ins
@router.get("/calls/preflight", tags=["calls"], summary="Pre-call screen data: purpose, number, duration, allowance, consent text")
async def call_preflight(intervention_id: UUID | None = None, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        user = await users_repo.get_by_id(conn, p.id)
        iv = (await priority.get_intervention(conn, p.id, intervention_id) if intervention_id
              else await priority.current_intervention(conn, p.id))
    return await calls_svc.preflight(user, iv)


@router.post("/calls", tags=["calls"], status_code=201, summary="Place a consented AI check-in call (idempotent)")
async def create_call(body: CallCreate, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        user = await users_repo.get_by_id(conn, p.id)
    return await calls_svc.create_call(user, intervention_id=body.intervention_id, destination=body.destination,
                                       consent=body.consent, consent_version=body.consent_version,
                                       idempotency_key=body.idempotency_key, initiated_by=p.id)


@router.post("/calls/simulate", tags=["calls"], status_code=201,
             summary="Labelled simulated call for synthetic demo profiles (no phone call placed)")
async def simulate_call(body: SimulateCallIn, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        user = await users_repo.get_by_id(conn, p.id)
    return await postcall.simulate_completed_call(user, body.intervention_id, body.idempotency_key)


@router.get("/calls", tags=["calls"])
async def list_calls(p: Principal = Depends(get_principal)):
    return {"items": await calls_svc.list_calls(p.id)}


@router.get("/calls/{call_id}", tags=["calls"],
            summary="Call state (provider, transcript and extraction states are separate). Reconciles if stale.")
async def get_call(call_id: UUID, p: Principal = Depends(get_principal)):
    call = await calls_svc.get_call(p.id, call_id)
    async with transaction() as conn:
        plan = await conn.fetchrow("select id, status::text as status from action_plans where call_id = $1 and status <> 'abandoned' "
                                   "order by created_at desc limit 1", call_id)
    return {**call, "destination": calls_svc.mask(call["destination"]) if not call["is_simulated"] else "SIMULATED",
            "plan": dict(plan) if plan else None}


@router.get("/calls/{call_id}/transcript", tags=["calls"])
async def get_transcript(call_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        t = await conn.fetchrow("""select t.raw_text, t.is_complete, t.provider_summary, t.recording_url, t.created_at, c.is_simulated
                                   from transcripts t join calls c on c.id = t.call_id where c.id = $1 and c.user_id = $2""",
                                call_id, p.id)
    if not t:
        raise NotFound("No transcript for this call")
    return dict(t)


@router.post("/calls/{call_id}/cancel", tags=["calls"])
async def cancel_call(call_id: UUID, p: Principal = Depends(get_principal)):
    return await calls_svc.cancel_call(p.id, call_id)


@router.post("/calls/{call_id}/extract", tags=["calls"], summary="Request extraction again (e.g. after a failure)")
async def retry_extraction(call_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        owns = await conn.fetchval("select extraction_status::text from calls where id = $1 and user_id = $2", call_id, p.id)
    if owns is None:
        raise NotFound("Call not found")
    if owns == "running":
        raise AppError("Extraction is already running", code="extraction_running")
    return await postcall.run_extraction(call_id, force=True)


# ------------------------------------------------------------------ action plans
@router.get("/plans", tags=["plans"])
async def list_plans(status: str | None = None, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await plans_svc.list_plans(conn, p.id, status)}


@router.post("/plans", tags=["plans"], status_code=201, summary="Create a plan manually")
async def create_plan(body: PlanCreate, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await plans_svc.create_manual_plan(conn, p.id, body.model_dump(mode="json") | {"due_date": body.due_date})


@router.get("/plans/{plan_id}", tags=["plans"], summary="Plan with original AI output and edit history")
async def get_plan(plan_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await plans_svc.get_plan(conn, p.id, plan_id)


@router.patch("/plans/{plan_id}", tags=["plans"], summary="Correct the AI summary/plan before accepting")
async def patch_plan(plan_id: UUID, body: PlanPatch, p: Principal = Depends(get_principal)):
    patch = body.model_dump(exclude_unset=True, mode="json")
    if "due_date" in patch:
        patch["due_date"] = body.due_date
    if "follow_up_at" in patch:
        patch["follow_up_at"] = body.follow_up_at
    async with transaction() as conn:
        return await plans_svc.edit_plan(conn, p.id, plan_id, patch)


@router.post("/plans/{plan_id}/accept", tags=["plans"])
async def accept_plan(plan_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await plans_svc.set_plan_status(conn, p.id, plan_id, "accepted")


@router.post("/plans/{plan_id}/complete", tags=["plans"])
async def complete_plan(plan_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await plans_svc.set_plan_status(conn, p.id, plan_id, "completed")


@router.post("/plans/{plan_id}/abandon", tags=["plans"])
async def abandon_plan(plan_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return await plans_svc.set_plan_status(conn, p.id, plan_id, "abandoned")


@router.post("/plans/{plan_id}/share", tags=["plans"],
             summary="Explicitly share a plan with authorized staff inside SAGE (no external message is sent)")
async def share_plan(plan_id: UUID, body: ShareIn, p: Principal = Depends(get_principal)):
    if not body.confirm:
        raise AppError("Sharing requires explicit confirmation", code="confirmation_required")
    async with transaction() as conn:
        return await plans_svc.share_plan(conn, p, plan_id, body.include_transcript)


@router.get("/shares", tags=["plans"], summary="Cases I have shared")
async def my_shares(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            "select id, plan_id, status::text as status, include_transcript, assigned_to is not null as assigned, created_at "
            "from cases where student_id = $1 and revoked_at is null order by created_at desc", p.id))}


@router.delete("/shares/{case_id}", tags=["plans"], summary="Revoke a share")
async def revoke_share(case_id: UUID, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        await plans_svc.revoke_share(conn, p, case_id)
    return {"revoked": True}
