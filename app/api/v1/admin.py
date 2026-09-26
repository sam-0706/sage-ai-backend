"""Superadmin operations (PRD Epic 8). Every high-risk action writes an audit event. Secrets are never returned."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.auth.deps import Principal, require_superadmin
from app.core.config import get_settings
from app.core.errors import AppError, NotFound
from app.domain.schemas import (AdminUserPatch, AssignIn, DirectoryIn, FlagIn, GrantIn, InviteIn, KnowledgeTextIn,
                                KnowledgeUrlIn, RoleDecisionIn, TemplateIn)
from app.repositories import audit, flags
from app.repositories import users as users_repo
from app.repositories.db import row, rows, transaction
from app.services import billing, calls as calls_svc, entitlements, knowledge
from app.services import plans as plans_svc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/overview", summary="Beta health at a glance")
async def overview(_: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        users = rows(await conn.fetch("select mode::text as mode, role::text as role, status::text as status, count(*) as n "
                                      "from users where is_demo = false group by 1,2,3 order by 1,2,3"))
        calls = rows(await conn.fetch("select status::text as status, count(*) as n, coalesce(sum(duration_seconds),0) as seconds "
                                      "from calls group by 1"))
        ai = rows(await conn.fetch("""select task, status, count(*) as n, avg(latency_ms)::int as avg_latency_ms,
                                             sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens
                                      from ai_runs where created_at > now() - interval '30 days' group by 1,2 order by 1,2"""))
        plans = rows(await conn.fetch("select status::text as status, count(*) as n from action_plans group by 1"))
        kb = await conn.fetchrow("select count(*) as sources, coalesce(sum(chunk_count),0) as chunks from knowledge_sources where status='ready'")
        activated = await conn.fetchval("select count(*) from users where is_demo = false and clerk_user_id is not null")
    s = get_settings()
    return {"users": users, "activated_users": activated, "calls": calls, "ai_runs_30d": ai, "plans": plans,
            "knowledge": dict(kb),
            "config": {"env": s.app_env, "openai_model": s.openai_model, "voice_agent_configured": bool(s.omnidim_agent_id),
                       "razorpay_test_mode": s.razorpay_test_mode, "razorpay_webhook_configured": bool(s.razorpay_webhook_secret)}}


# ------------------------------------------------------------------ users
@router.get("/users")
async def list_users(q: str | None = None, role: str | None = None, mode: str | None = None, status: str | None = None,
                     limit: int = Query(50, le=200), offset: int = 0, _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        items, total = await users_repo.list_users(conn, q=q, role=role, mode=mode, status=status, limit=limit, offset=offset)
    for u in items:
        u["signed_in"] = u.pop("clerk_user_id") is not None
    return {"items": items, "total": total}


@router.get("/users/{user_id}")
async def get_user(user_id: UUID, _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        u = await users_repo.get_by_id(conn, user_id)
        if not u:
            raise NotFound("User not found")
        u["signed_in"] = u.pop("clerk_user_id") is not None
        sub = await entitlements.active_subscription(conn, user_id)
        counts = await conn.fetchrow(
            """select (select count(*) from signals where user_id=$1) as signals,
                      (select count(*) from interventions where user_id=$1) as interventions,
                      (select count(*) from calls where user_id=$1) as calls,
                      (select count(*) from action_plans where user_id=$1) as plans""", user_id)
    return {"user": u, "subscription": entitlements.summarize(sub), "counts": dict(counts)}


@router.patch("/users/{user_id}", summary="Change role/status/institution (audited)")
async def patch_user(user_id: UUID, body: AdminUserPatch, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        before = await users_repo.get_by_id(conn, user_id)
        if not before:
            raise NotFound("User not found")
        if body.role and user_id == admin.id and body.role != "superadmin":
            raise AppError("You cannot remove your own superadmin role", code="self_demotion")
        if body.role:
            await users_repo.set_role(conn, user_id, body.role)
        if body.status:
            await users_repo.set_status(conn, user_id, body.status)
        if body.institution_id is not None:
            await conn.execute("update users set institution_id = $2 where id = $1", user_id, body.institution_id)
        after = await users_repo.get_by_id(conn, user_id)
        await audit.record(conn, "admin.user_changed", actor_id=admin.id, target_type="user", target_id=user_id,
                           metadata={"reason": body.reason,
                                     "before": {k: str(before[k]) for k in ("role", "status", "institution_id")},
                                     "after": {k: str(after[k]) for k in ("role", "status", "institution_id")}})
    after.pop("clerk_user_id", None)
    return {"user": after}


@router.post("/users/invite", status_code=201, summary="Add a user to the invite list")
async def invite(body: InviteIn, admin: Principal = Depends(require_superadmin)):
    phone = calls_svc.normalize_number(body.phone) if body.phone else None
    async with transaction() as conn:
        uid = await conn.fetchval(
            """insert into users (email, full_name, phone, mode, role, status, onboarded_at)
               values (lower($1), $2, $3, $4::experience_mode, $5::access_role, 'invited', now())
               on conflict (email) do nothing returning id""", body.email, body.full_name, phone, body.mode, body.role)
        if not uid:
            raise AppError("A user with this email already exists", code="duplicate_email", status_code=409)
        await conn.execute("insert into goal_profiles (user_id, mode) values ($1, $2::experience_mode)", uid, body.mode)
        await billing.grant_plan(conn, uid, body.plan_code, source="admin_invite", actor_id=admin.id)
        await audit.record(conn, "admin.user_invited", actor_id=admin.id, target_type="user", target_id=uid,
                           metadata={"role": body.role, "plan": body.plan_code})
    return {"id": uid}


@router.post("/users/{user_id}/grant", summary="Grant a plan or extra credits (audited)")
async def grant(user_id: UUID, body: GrantIn, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        if body.plan_code:
            await billing.grant_plan(conn, user_id, body.plan_code, source="admin_grant", actor_id=admin.id)
        extra_v = int((body.extra_voice_minutes or 0) * 60)
        if extra_v or body.extra_ai_requests or body.extra_chat_messages:
            n = await conn.execute(
                """update subscriptions set voice_seconds_allowance = voice_seconds_allowance + $2,
                          ai_requests_allowance = ai_requests_allowance + $3, chat_messages_allowance = chat_messages_allowance + $4
                   where user_id = $1 and status = 'active'""",
                user_id, extra_v, body.extra_ai_requests or 0, body.extra_chat_messages or 0)
            if n.endswith(" 0"):
                raise AppError("User has no active subscription", code="no_subscription")
        await audit.record(conn, "admin.credits_granted", actor_id=admin.id, target_type="user", target_id=user_id,
                           metadata=body.model_dump())
        sub = await entitlements.active_subscription(conn, user_id)
    return {"subscription": entitlements.summarize(sub)}


@router.get("/role-requests")
async def role_requests(status: str = "pending", _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            """select r.id, r.user_id, u.email::text as email, u.full_name, r.requested_role::text as requested_role, r.reason,
                      r.status, r.created_at from role_change_requests r join users u on u.id = r.user_id
               where r.status = $1 order by r.created_at""", status))}


@router.post("/role-requests/{request_id}/decision")
async def decide_role(request_id: UUID, body: RoleDecisionIn, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        req = row(await conn.fetchrow("select *, requested_role::text as rr from role_change_requests where id = $1 and status = 'pending' "
                                      "for update", request_id))
        if not req:
            raise NotFound("Pending request not found")
        await conn.execute("update role_change_requests set status = $2, reviewed_by = $3, reviewed_at = now() where id = $1",
                           request_id, "approved" if body.approve else "rejected", admin.id)
        if body.approve:
            await users_repo.set_role(conn, req["user_id"], req["rr"])
        await audit.record(conn, "admin.role_decision", actor_id=admin.id, target_type="user", target_id=req["user_id"],
                           metadata={"approved": body.approve, "role": req["rr"], "note": body.note})
    return {"status": "approved" if body.approve else "rejected"}


# ------------------------------------------------------------------ configuration
@router.get("/flags")
async def get_flags(_: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch("select * from feature_flags order by key"))}


@router.put("/flags/{key}")
async def set_flag(key: str, body: FlagIn, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        n = await conn.execute("update feature_flags set enabled = $2, updated_at = now() where key = $1", key, body.enabled)
        if n.endswith(" 0"):
            raise NotFound("Flag not found")
        await audit.record(conn, "admin.flag_changed", actor_id=admin.id, target_type="feature_flag", target_id=key,
                           metadata={"enabled": body.enabled})
    flags.invalidate()
    return {"key": key, "enabled": body.enabled}


@router.put("/advisor-templates/{key}")
async def upsert_template(key: str, body: TemplateIn, admin: Principal = Depends(require_superadmin)):
    if key != body.key:
        raise AppError("Key mismatch", code="key_mismatch")
    async with transaction() as conn:
        await conn.execute(
            """insert into advisor_templates (key, modes, category, title, description, help_types, prep_questions, share_checklist, sort_order)
               values ($1,$2::experience_mode[],$3,$4,$5,$6,$7,$8,$9)
               on conflict (key) do update set modes=excluded.modes, category=excluded.category, title=excluded.title,
                 description=excluded.description, help_types=excluded.help_types, prep_questions=excluded.prep_questions,
                 share_checklist=excluded.share_checklist, sort_order=excluded.sort_order""",
            body.key, body.modes, body.category, body.title, body.description, body.help_types, body.prep_questions,
            body.share_checklist, body.sort_order)
        await audit.record(conn, "admin.template_upserted", actor_id=admin.id, target_type="advisor_template", target_id=key)
    return {"key": key}


# ------------------------------------------------------------------ knowledge base & directory
@router.get("/knowledge")
async def list_knowledge(_: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": await knowledge.list_sources(conn)}


@router.post("/knowledge/text", status_code=201)
async def add_text(body: KnowledgeTextIn, admin: Principal = Depends(require_superadmin)):
    return await knowledge.ingest(title=body.title, kind="text", text=body.text, category=body.category, audience=body.audience,
                                  institution_id=body.institution_id, is_demo=body.is_demo, created_by=admin.id)


@router.post("/knowledge/url", status_code=201, summary="Fetch a public web page or PDF and index it")
async def add_url(body: KnowledgeUrlIn, admin: Principal = Depends(require_superadmin)):
    text = await knowledge.fetch_url_text(body.url)
    return await knowledge.ingest(title=body.title or body.url, kind="url", text=text, url=body.url, category=body.category,
                                  audience=body.audience, institution_id=body.institution_id, created_by=admin.id)


@router.post("/knowledge/upload", status_code=201, summary="Upload a PDF, .txt or .md file and index it")
async def upload(file: UploadFile = File(...), title: str = Form(...), category: str | None = Form(None),
                 is_demo: bool = Form(False), admin: Principal = Depends(require_superadmin)):
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise AppError("File too large (max 8 MB)", code="file_too_large")
    name = (file.filename or "").lower()
    if name.endswith(".pdf") or file.content_type == "application/pdf":
        text, kind = knowledge.pdf_to_text(data), "pdf"
    elif name.endswith((".txt", ".md")):
        text, kind = data.decode("utf-8", errors="ignore"), "text"
    else:
        raise AppError("Supported files: .pdf, .txt, .md", code="unsupported_file")
    return await knowledge.ingest(title=title, kind=kind, text=text, category=category, is_demo=is_demo, created_by=admin.id)


@router.delete("/knowledge/{source_id}")
async def delete_knowledge(source_id: UUID, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        await knowledge.delete_source(conn, source_id, admin.id)
    return {"deleted": True}


@router.post("/directory", status_code=201)
async def add_directory(body: DirectoryIn, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        did = await conn.fetchval(
            """insert into faculty_directory (institution_id, user_id, name, kind, department, designation, subjects, expertise,
                                              office_hours, contact_hint, bio, is_demo, label)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, case when $12 then 'Synthetic demonstration record' else 'Verified by SAGE admin' end)
               returning id""",
            body.institution_id, body.user_id, body.name, body.kind, body.department, body.designation, body.subjects,
            body.expertise, body.office_hours, body.contact_hint, body.bio, body.is_demo)
        await audit.record(conn, "admin.directory_added", actor_id=admin.id, target_type="directory", target_id=did)
    return {"id": did}


@router.delete("/directory/{entry_id}")
async def delete_directory(entry_id: UUID, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        n = await conn.execute("delete from faculty_directory where id = $1", entry_id)
        if n.endswith(" 0"):
            raise NotFound("Entry not found")
        await audit.record(conn, "admin.directory_deleted", actor_id=admin.id, target_type="directory", target_id=entry_id)
    return {"deleted": True}


# ------------------------------------------------------------------ diagnostics (no secrets, no raw provider bodies)
@router.get("/ai-runs")
async def ai_runs(task: str | None = None, status: str | None = None, limit: int = Query(50, le=200),
                  _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            """select id, user_id, task, prompt_version, model, provider_request_id, correlation_id, latency_ms, input_tokens,
                      output_tokens, status, validation_ok, error, created_at from ai_runs
               where ($1::text is null or task = $1) and ($2::text is null or status = $2)
               order by created_at desc limit $3""", task, status, limit))}


@router.get("/calls")
async def admin_calls(status: str | None = None, limit: int = Query(50, le=200), _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        items = rows(await conn.fetch(
            f"""select {calls_svc.CALL_COLS} from calls where ($1::text is null or status::text = $1)
                order by created_at desc limit $2""", status, limit))
    for c in items:
        c["destination"] = calls_svc.mask(c["destination"])
    return {"items": items}


@router.post("/calls/{call_id}/reconcile", summary="Force provider reconciliation for one call")
async def reconcile(call_id: UUID, admin: Principal = Depends(require_superadmin)):
    call = await calls_svc.reconcile_call(call_id)
    if not call:
        raise NotFound("Call not found")
    call["destination"] = calls_svc.mask(call["destination"])
    return call


@router.get("/webhook-events")
async def webhook_events(provider: str | None = None, limit: int = Query(50, le=200), _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            """select id, provider, dedupe_key, status, error, received_at, processed_at from webhook_events
               where ($1::text is null or provider = $1) order by received_at desc limit $2""", provider, limit))}


@router.get("/audit")
async def audit_log(action: str | None = None, target_id: str | None = None, limit: int = Query(100, le=500),
                    _: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            """select id, actor_id, action, target_type, target_id, metadata, correlation_id, created_at from audit_events
               where ($1::text is null or action like $1 || '%') and ($2::text is null or target_id = $2)
               order by id desc limit $3""", action, target_id, limit))}


@router.get("/cases")
async def admin_cases(status: str | None = None, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        return {"items": await plans_svc.list_cases(conn, admin, status)}


@router.post("/cases/{case_id}/assign")
async def assign_case(case_id: UUID, body: AssignIn, admin: Principal = Depends(require_superadmin)):
    async with transaction() as conn:
        await plans_svc.assign_case(conn, admin, case_id, body.staff_user_id)
    return {"assigned": True}
