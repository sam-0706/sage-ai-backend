from fastapi import APIRouter, Depends

from app.auth.deps import Principal, get_principal
from app.core.errors import Conflict
from app.domain.schemas import MeUpdate, RoleRequestIn
from app.repositories import audit, flags
from app.repositories import users as users_repo
from app.repositories.db import transaction
from app.services import calls as calls_svc
from app.services import entitlements, priority
from app.services import plans as plans_svc
from app.services import profile as profile_svc

router = APIRouter(tags=["me"])


@router.get("/me", summary="Current user, entitlements and feature flags")
async def get_me(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        user = await users_repo.get_by_id(conn, p.id)
        sub = await entitlements.active_subscription(conn, p.id)
        prof = await profile_svc.get_profile(conn, p.id, p.mode)
        pending = await conn.fetchrow("select requested_role, created_at from role_change_requests "
                                      "where user_id = $1 and status = 'pending'", p.id)
        f = await flags.all_flags(conn)
    user.pop("clerk_user_id", None)
    return {"user": user, "subscription": entitlements.summarize(sub), "profile_completeness": prof["completeness"],
            "missing_profile_fields": prof["missing_fields"], "demo_profile_key": prof["demo_profile_key"],
            "pending_role_request": dict(pending) if pending else None, "flags": f,
            "needs_onboarding": user["onboarded_at"] is None or not prof["fields"] or list(prof["fields"]) == ["segment"]}


@router.patch("/me", summary="Update own profile basics or experience mode")
async def patch_me(body: MeUpdate, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        fields = body.model_dump(exclude_unset=True)
        if "phone" in fields and fields["phone"]:
            fields["phone"] = calls_svc.normalize_number(fields["phone"])
        user = await users_repo.update_self(conn, p.id, fields)
        if "mode" in fields:
            await conn.execute("update goal_profiles set mode = $2::experience_mode where user_id = $1", p.id, fields["mode"])
        await audit.record(conn, "user.updated_self", actor_id=p.id, target_type="user", target_id=p.id,
                           metadata={"fields": list(fields)})
    user.pop("clerk_user_id", None)
    return {"user": user}


@router.post("/me/role-requests", status_code=201, summary="Request faculty/advisor access (requires superadmin approval)")
async def request_role(body: RoleRequestIn, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        if await conn.fetchval("select 1 from role_change_requests where user_id = $1 and status = 'pending'", p.id):
            raise Conflict("You already have a pending role request")
        rid = await conn.fetchval("insert into role_change_requests (user_id, requested_role, reason) values ($1,$2::access_role,$3) "
                                  "returning id", p.id, body.requested_role, body.reason)
        await audit.record(conn, "role.requested", actor_id=p.id, target_type="role_request", target_id=rid,
                           metadata={"requested_role": body.requested_role})
    return {"id": rid, "status": "pending"}


@router.get("/home", summary="Home screen: one clear priority, next items, active plan, allowance")
async def home(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        iv = await priority.current_intervention(conn, p.id)
        active_plans = await plans_svc.list_plans(conn, p.id, "accepted")
        drafts = await plans_svc.list_plans(conn, p.id, "draft")
        sub = await entitlements.active_subscription(conn, p.id)
        signal_count = await conn.fetchval("select count(*) from signals where user_id = $1 and status = 'active'", p.id)
        last_call = await conn.fetchrow(f"select {calls_svc.CALL_COLS} from calls where user_id = $1 order by created_at desc limit 1", p.id)
    return {"priority": iv, "draft_plans": drafts[:3], "active_plans": active_plans[:5], "active_signal_count": signal_count,
            "last_call": dict(last_call) if last_call else None, "subscription": entitlements.summarize(sub),
            "state": "needs_setup" if signal_count == 0 else ("attention_needed" if iv else "refresh_available")}


@router.delete("/me/data", summary="Delete own profile, AI outputs and call artifacts")
async def delete_my_data(p: Principal = Depends(get_principal)):
    """PRD: support deletion of profile, AI outputs and call artifacts, keeping minimal payment and security audit records."""
    async with transaction() as conn:
        for table in ("chat_sessions", "action_plans", "cases", "calls", "interventions", "signals", "goal_profiles"):
            col = "student_id" if table == "cases" else "user_id"
            await conn.execute(f"delete from {table} where {col} = $1", p.id)
        await conn.execute("update ai_runs set user_id = null where user_id = $1", p.id)
        await audit.record(conn, "user.data_deleted", actor_id=p.id, target_type="user", target_id=p.id)
    return {"deleted": True, "retained": ["account identity", "subscription and payment records", "security audit events"]}
