"""Onboarding for waitlisted testers: everything we already know from the waitlist is pre-filled."""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.deps import Principal, get_principal
from app.domain.schemas import Mode
from app.repositories import audit
from app.repositories import users as users_repo
from app.repositories.db import transaction
from app.services import calls as calls_svc
from app.services import entitlements
from app.services import profile as profile_svc

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

MODE_LABELS = {"student": "Student", "professional": "Working professional", "founder": "Founder"}


class OnboardingIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    mode: Mode
    phone: str | None = Field(default=None, max_length=20)
    profile: dict[str, Any] = Field(default_factory=dict)
    goals: list[str] = Field(default_factory=list, max_length=10)
    interests: list[str] = Field(default_factory=list, max_length=12)   # e.g. exam_prep, auto_apply, check_ins
    call_consent: bool = False
    preferred_call_window: str | None = Field(default=None, max_length=60)


@router.get("", summary="Onboarding state with values pre-filled from the waitlist")
async def get_onboarding(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        user = await users_repo.get_by_id(conn, p.id)
        prof = await profile_svc.get_profile(conn, p.id, p.mode)
        inst = await conn.fetchval("select name from institutions where id = $1", user["institution_id"]) if user["institution_id"] else None
        completed = await conn.fetchval("select onboarding_completed_at from users where id = $1", p.id)
        sub = await entitlements.active_subscription(conn, p.id)
    data = {k: v["value"] for k, v in prof["fields"].items() if k != "segment"}
    if inst and p.mode == "student":
        data.setdefault("institution_name", inst)
    return {
        "completed": completed is not None,
        "completed_at": completed,
        "prefill": {
            "full_name": user["full_name"], "email": user["email"], "phone": user["phone"], "mode": user["mode"],
            "waitlist_segment": user["waitlist_segment"], "institution": inst, "profile": data,
            "goals": prof["consent"].get("goals", []) if isinstance(prof.get("consent"), dict) else [],
            "call_consent": bool((prof.get("consent") or {}).get("voice_calls")),
        },
        "modes": [{"value": k, "label": v} for k, v in MODE_LABELS.items()],
        "profile_fields": profile_svc.MODE_FIELDS,
        "required_fields": profile_svc.REQUIRED_FOR_CONFIDENCE,
        "plan": entitlements.summarize(sub),
    }


@router.post("", summary="Complete (or update) onboarding")
async def complete_onboarding(body: OnboardingIn, p: Principal = Depends(get_principal)):
    phone = calls_svc.normalize_number(body.phone) if body.phone else None
    consent = {"voice_calls": body.call_consent, "preferred_call_window": body.preferred_call_window,
               "goals": body.goals, "interests": body.interests}
    async with transaction() as conn:
        await users_repo.update_self(conn, p.id, {"full_name": body.full_name, "mode": body.mode,
                                                  **({"phone": phone} if phone else {})})
        prof = await profile_svc.upsert_profile(conn, p.id, body.mode, body.profile, None, consent)
        await conn.execute("update users set onboarding_completed_at = coalesce(onboarding_completed_at, now()), "
                           "status = case when status = 'invited' then 'active'::user_status else status end where id = $1", p.id)
        await audit.record(conn, "onboarding.completed", actor_id=p.id, target_type="user", target_id=p.id,
                           metadata={"mode": body.mode, "interests": body.interests, "call_consent": body.call_consent})
        user = await users_repo.get_by_id(conn, p.id)
    user.pop("clerk_user_id", None)
    return {"completed": True, "user": user, "profile": prof}
