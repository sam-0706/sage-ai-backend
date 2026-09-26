"""Action plans (PRD Epic 5/6) and the faculty/advisor case workspace (Epic 7).

Access rules for staff (enforced here, never by the client):
- superadmin: all cases (transcripts only where the student allowed it)
- faculty/advisor: cases assigned to them, or cases the student shared with their institution
- nobody sees a revoked share
"""
from datetime import date
from uuid import UUID

import asyncpg

from app.auth.deps import Principal
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.repositories import audit
from app.repositories.db import row, rows

PLAN_COLS = """id, user_id, intervention_id, call_id, extraction_id, status::text as status, summary, issue, root_cause, actions,
               owner, due_date, risk_level, confidence, escalation, advisor_template_key, follow_up_at, is_user_edited,
               accepted_at, created_at, updated_at"""
EDITABLE = {"summary", "issue", "root_cause", "actions", "owner", "due_date", "risk_level", "advisor_template_key", "follow_up_at"}


async def list_plans(conn: asyncpg.Connection, user_id: UUID, status: str | None = None) -> list[dict]:
    if status:
        return rows(await conn.fetch(f"select {PLAN_COLS} from action_plans where user_id = $1 and status = $2::plan_status "
                                     "order by created_at desc", user_id, status))
    return rows(await conn.fetch(f"select {PLAN_COLS} from action_plans where user_id = $1 and status <> 'abandoned' "
                                 "order by created_at desc", user_id))


async def get_plan(conn: asyncpg.Connection, user_id: UUID, plan_id: UUID) -> dict:
    p = row(await conn.fetchrow(f"select {PLAN_COLS} from action_plans where id = $1 and user_id = $2", plan_id, user_id))
    if not p:
        raise NotFound("Plan not found")
    if p["extraction_id"]:
        ext = await conn.fetchrow("select output, created_at from extractions where id = $1", p["extraction_id"])
        p["original_ai_output"] = ext["output"] if ext else None
    p["revisions"] = rows(await conn.fetch(
        "select id, before, after, created_at from plan_revisions where plan_id = $1 order by created_at", plan_id))
    return p


async def create_manual_plan(conn: asyncpg.Connection, user_id: UUID, data: dict) -> dict:
    return row(await conn.fetchrow(
        f"""insert into action_plans (user_id, intervention_id, summary, issue, root_cause, actions, owner, due_date, risk_level,
                                      advisor_template_key, is_user_edited)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true) returning {PLAN_COLS}""",
        user_id, data.get("intervention_id"), data["summary"], data.get("issue"), data.get("root_cause"),
        data.get("actions") or [], data.get("owner"), data.get("due_date"), data.get("risk_level"),
        data.get("advisor_template_key")))


async def edit_plan(conn: asyncpg.Connection, user_id: UUID, plan_id: UUID, patch: dict) -> dict:
    current = row(await conn.fetchrow(f"select {PLAN_COLS} from action_plans where id = $1 and user_id = $2 for update",
                                      plan_id, user_id))
    if not current:
        raise NotFound("Plan not found")
    if current["status"] in ("completed", "abandoned"):
        raise Conflict("This plan can no longer be edited")
    fields = {k: v for k, v in patch.items() if k in EDITABLE}
    if not fields:
        return await get_plan(conn, user_id, plan_id)
    if fields.get("advisor_template_key"):
        if not await conn.fetchval("select 1 from advisor_templates where key = $1", fields["advisor_template_key"]):
            raise AppError("Unknown advisor template", code="invalid_template")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    await conn.execute(f"update action_plans set {sets}, is_user_edited = true where id = $1", plan_id, *fields.values())
    before = {k: current[k] for k in fields}
    await conn.execute("insert into plan_revisions (plan_id, editor_id, before, after) values ($1,$2,$3,$4)",
                       plan_id, user_id, _jsonable(before), _jsonable(fields))
    await audit.record(conn, "plan.edited", actor_id=user_id, target_type="plan", target_id=plan_id,
                       metadata={"fields": list(fields)})
    return await get_plan(conn, user_id, plan_id)


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


async def set_plan_status(conn: asyncpg.Connection, user_id: UUID, plan_id: UUID, status: str) -> dict:
    current = row(await conn.fetchrow("select status::text as status, intervention_id from action_plans where id = $1 and user_id = $2",
                                      plan_id, user_id))
    if not current:
        raise NotFound("Plan not found")
    allowed = {"accepted": ("draft",), "completed": ("accepted",), "abandoned": ("draft", "accepted")}
    if current["status"] not in allowed[status]:
        raise Conflict(f"Cannot move a {current['status']} plan to {status}")
    await conn.execute(
        """update action_plans set status = $2::plan_status,
               accepted_at = case when $2 = 'accepted' then now() else accepted_at end where id = $1""", plan_id, status)
    if status == "accepted" and current["intervention_id"]:
        await conn.execute("update interventions set status = 'plan_accepted' where id = $1", current["intervention_id"])
    if status == "completed" and current["intervention_id"]:
        await conn.execute("update interventions set status = 'closed' where id = $1", current["intervention_id"])
    await audit.record(conn, f"plan.{status}", actor_id=user_id, target_type="plan", target_id=plan_id)
    return await get_plan(conn, user_id, plan_id)


# ------------------------------------------------------------------ sharing (explicit student action)
async def share_plan(conn: asyncpg.Connection, user: Principal, plan_id: UUID, include_transcript: bool) -> dict:
    plan = row(await conn.fetchrow("select id, intervention_id, status::text as status from action_plans where id = $1 and user_id = $2",
                                   plan_id, user.id))
    if not plan:
        raise NotFound("Plan not found")
    existing = row(await conn.fetchrow("select * from cases where plan_id = $1 and student_id = $2 and revoked_at is null",
                                       plan_id, user.id))
    if existing:
        await conn.execute("update cases set shared_by_student = true, include_transcript = $2 where id = $1",
                           existing["id"], include_transcript)
        case_id = existing["id"]
    else:
        case_id = await conn.fetchval(
            """insert into cases (student_id, plan_id, intervention_id, shared_by_student, include_transcript)
               values ($1,$2,$3,true,$4) returning id""", user.id, plan_id, plan["intervention_id"], include_transcript)
    await audit.record(conn, "plan.shared", actor_id=user.id, target_type="case", target_id=case_id,
                       metadata={"include_transcript": include_transcript, "auto_message_sent": False})
    return {"case_id": case_id, "shared": True, "include_transcript": include_transcript,
            "notice": "Shared inside SAGE AI only. No email, SMS or message was sent to anyone."}


async def revoke_share(conn: asyncpg.Connection, user: Principal, case_id: UUID) -> None:
    n = await conn.execute("update cases set revoked_at = now() where id = $1 and student_id = $2 and revoked_at is null",
                           case_id, user.id)
    if n.endswith(" 0"):
        raise NotFound("Shared case not found")
    await audit.record(conn, "plan.share_revoked", actor_id=user.id, target_type="case", target_id=case_id)


# ------------------------------------------------------------------ staff workspace
def _case_scope(p: Principal) -> tuple[str, list]:
    if p.is_superadmin:
        return "c.revoked_at is null", []
    return ("""c.revoked_at is null and (c.assigned_to = $1 or
               (c.shared_by_student and $2::uuid is not null and s.institution_id = $2::uuid))""",
            [p.id, p.institution_id])


async def list_cases(conn: asyncpg.Connection, p: Principal, status: str | None) -> list[dict]:
    where, args = _case_scope(p)
    if status:
        args.append(status)
        where += f" and c.status = ${len(args)}::case_status"
    return rows(await conn.fetch(
        f"""select c.id, c.status::text as status, c.follow_up_at, c.shared_by_student, c.include_transcript, c.created_at,
                   c.assigned_to, s.id as student_id, s.full_name as student_name, s.mode::text as student_mode,
                   ap.summary, ap.issue, ap.root_cause, ap.risk_level, ap.due_date, ap.status::text as plan_status,
                   i.title as intervention_title
            from cases c join users s on s.id = c.student_id
            left join action_plans ap on ap.id = c.plan_id
            left join interventions i on i.id = c.intervention_id
            where {where} order by c.updated_at desc limit 200""", *args))


async def get_case(conn: asyncpg.Connection, p: Principal, case_id: UUID) -> dict:
    where, args = _case_scope(p)
    args.append(case_id)
    c = row(await conn.fetchrow(
        f"""select c.*, c.status::text as status, s.full_name as student_name, s.mode::text as student_mode
            from cases c join users s on s.id = c.student_id where {where} and c.id = ${len(args)}""", *args))
    if not c:
        raise NotFound("Case not found")  # 404 not 403: do not reveal that unrelated cases exist
    plan = row(await conn.fetchrow(f"select {PLAN_COLS} from action_plans where id = $1", c["plan_id"])) if c["plan_id"] else None
    iv = row(await conn.fetchrow("select title, reason, evidence, category from interventions where id = $1",
                                 c["intervention_id"])) if c["intervention_id"] else None
    transcript = None
    if c["include_transcript"] and plan and plan["call_id"]:
        t = await conn.fetchrow("select raw_text from transcripts where call_id = $1", plan["call_id"])
        transcript = t["raw_text"] if t else None
    notes = rows(await conn.fetch(
        """select n.id, n.kind, n.body, n.created_at, u.full_name as author_name, u.role::text as author_role
           from case_notes n join users u on u.id = n.author_id where n.case_id = $1 order by n.created_at""", case_id))
    await audit.record(conn, "case.viewed", actor_id=p.id, target_type="case", target_id=case_id,
                       metadata={"transcript_visible": transcript is not None})
    return {"case": c, "plan": plan, "intervention": iv, "transcript": transcript,
            "transcript_access": "shared_by_student" if transcript else "not_shared", "notes": notes}


async def add_case_note(conn: asyncpg.Connection, p: Principal, case_id: UUID, kind: str, body: str,
                        status: str | None, follow_up_at) -> dict:
    await get_case(conn, p, case_id)  # permission check
    await conn.execute("insert into case_notes (case_id, author_id, kind, body) values ($1,$2,$3,$4)", case_id, p.id, kind, body)
    if status or follow_up_at:
        await conn.execute("update cases set status = coalesce($2::case_status, status), follow_up_at = coalesce($3, follow_up_at) "
                           "where id = $1", case_id, status, follow_up_at)
    await audit.record(conn, "case.updated", actor_id=p.id, target_type="case", target_id=case_id,
                       metadata={"kind": kind, "status": status})
    return await get_case(conn, p, case_id)


async def assign_case(conn: asyncpg.Connection, admin: Principal, case_id: UUID, staff_id: UUID) -> None:
    staff = await conn.fetchrow("select role::text as role from users where id = $1", staff_id)
    if not staff or staff["role"] not in ("faculty", "advisor"):
        raise AppError("Cases can only be assigned to faculty or advisors", code="invalid_assignee")
    if not await conn.fetchval("select shared_by_student from cases where id = $1 and revoked_at is null", case_id):
        raise Forbidden("Only cases the student has shared can be assigned", code="not_shared")
    await conn.execute("update cases set assigned_to = $2 where id = $1", case_id, staff_id)
    await audit.record(conn, "case.assigned", actor_id=admin.id, target_type="case", target_id=case_id,
                       metadata={"assigned_to": str(staff_id)})
