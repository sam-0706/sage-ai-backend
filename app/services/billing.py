"""Plans and Razorpay test checkout (PRD Epic 9). Entitlements change only after server-side verification."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from app.auth.deps import Principal
from app.core.config import get_settings
from app.core.errors import AppError, Conflict, FeatureDisabled, Forbidden, NotFound
from app.integrations import razorpay
from app.repositories import audit, flags
from app.repositories.db import row, rows, transaction

TEST_LABEL = "TEST MODE — Razorpay test checkout. No real money is collected."
ORDER_COLS = """id, user_id, plan_code, amount, currency, status::text as status, razorpay_order_id, razorpay_payment_id,
                verified_at, is_test, error, created_at"""


async def list_plans(conn: asyncpg.Connection) -> list[dict]:
    return rows(await conn.fetch(
        """select code, name, description, price_inr_paise, price_usd_cents, period_days, voice_minutes, ai_requests,
                  chat_messages, features, is_purchasable from billing_plans where is_active order by sort_order"""))


async def grant_plan(conn: asyncpg.Connection, user_id: UUID, plan_code: str, *, source: str, actor_id: UUID | None) -> dict:
    plan = row(await conn.fetchrow("select * from billing_plans where code = $1", plan_code))
    if not plan:
        raise NotFound("Plan not found")
    await conn.execute("update subscriptions set status = 'expired', period_end = least(coalesce(period_end, now()), now()) "
                       "where user_id = $1 and status = 'active'", user_id)
    sub = row(await conn.fetchrow(
        """insert into subscriptions (user_id, plan_code, period_end, voice_seconds_allowance, ai_requests_allowance,
                                      chat_messages_allowance, autoapply_calls_allowance, is_test, source)
           values ($1,$2,$3,$4,$5,$6,$7,true,$8) returning *""",
        user_id, plan_code, datetime.now(UTC) + timedelta(days=plan["period_days"]), int(plan["voice_minutes"] * 60),
        plan["ai_requests"], plan["chat_messages"], plan["autoapply_calls"], source))
    await audit.record(conn, "subscription.granted", actor_id=actor_id, target_type="user", target_id=user_id,
                       metadata={"plan": plan_code, "source": source})
    return sub


async def create_order(p: Principal, plan_code: str, idempotency_key: str) -> dict:
    s = get_settings()
    async with transaction() as conn:
        if not await flags.is_enabled(conn, "payments_test_checkout"):
            raise FeatureDisabled("Test checkout is currently disabled")
        plan = row(await conn.fetchrow("select * from billing_plans where code = $1 and is_active", plan_code))
        if not plan or not plan["is_purchasable"]:
            raise AppError("This plan cannot be purchased online", code="plan_not_purchasable")
        order = row(await conn.fetchrow(
            f"""insert into payment_orders (user_id, plan_code, idempotency_key, amount, currency)
                values ($1,$2,$3,$4,'INR') on conflict (user_id, idempotency_key) do update set updated_at = now()
                returning {ORDER_COLS}""", p.id, plan_code, idempotency_key, plan["price_inr_paise"]))
    if order["plan_code"] != plan_code:
        raise Conflict("Idempotency key already used for a different plan")
    if not order["razorpay_order_id"]:
        rz = await razorpay.create_order(amount=order["amount"], currency="INR", receipt=str(order["id"]).replace("-", "")[:40],
                                         notes={"sage_order_id": str(order["id"]), "user_id": str(p.id), "plan": plan_code})
        async with transaction() as conn:
            order = row(await conn.fetchrow(f"update payment_orders set razorpay_order_id = $2 where id = $1 returning {ORDER_COLS}",
                                            order["id"], rz["id"]))
            await audit.record(conn, "payment.order_created", actor_id=p.id, target_type="payment_order", target_id=order["id"],
                               metadata={"plan": plan_code, "amount": order["amount"]})
    return {
        "order": order, "test_mode": s.razorpay_test_mode, "label": TEST_LABEL,
        "checkout": {"key": s.razorpay_key_id, "order_id": order["razorpay_order_id"], "amount": order["amount"], "currency": "INR",
                     "name": "SAGE AI", "description": f"{plan['name']} (test mode)",
                     "prefill": {"name": p.full_name or "", "email": p.email, "contact": p.phone or ""},
                     "notes": {"sage_order_id": str(order["id"])}},
    }


async def _settle(conn: asyncpg.Connection, order: dict, payment_id: str, source: str, actor_id: UUID | None) -> dict:
    """Idempotent: a paid order is granted exactly once."""
    if order["status"] == "paid":
        return order
    payment = await razorpay.fetch_payment(payment_id)
    ok = (payment.get("order_id") == order["razorpay_order_id"] and int(payment.get("amount", -1)) == order["amount"]
          and payment.get("status") in ("authorized", "captured"))
    if not ok:
        await conn.execute("update payment_orders set status = 'failed', error = $2 where id = $1", order["id"],
                           f"payment state mismatch: {payment.get('status')}")
        raise AppError("Payment could not be verified with the provider", code="payment_unverified")
    updated = row(await conn.fetchrow(
        f"""update payment_orders set status = 'paid', razorpay_payment_id = $2, verified_at = now()
            where id = $1 and status <> 'paid' returning {ORDER_COLS}""", order["id"], payment_id))
    if updated:  # we won the race — grant
        await grant_plan(conn, order["user_id"], order["plan_code"], source=source, actor_id=actor_id)
        await audit.record(conn, "payment.verified", actor_id=actor_id, target_type="payment_order", target_id=order["id"],
                           metadata={"payment_id": payment_id, "source": source, "test": True})
        return updated
    return row(await conn.fetchrow(f"select {ORDER_COLS} from payment_orders where id = $1", order["id"]))


async def verify_checkout(p: Principal, razorpay_order_id: str, razorpay_payment_id: str, signature: str) -> dict:
    async with transaction() as conn:
        order = row(await conn.fetchrow(f"select {ORDER_COLS} from payment_orders where razorpay_order_id = $1 for update",
                                        razorpay_order_id))
        if not order or order["user_id"] != p.id:
            raise NotFound("Order not found")
        if not razorpay.verify_payment_signature(razorpay_order_id, razorpay_payment_id, signature):
            await audit.record(conn, "payment.signature_invalid", actor_id=p.id, target_type="payment_order", target_id=order["id"])
            raise Forbidden("Payment signature verification failed", code="invalid_signature")
        order = await _settle(conn, order, razorpay_payment_id, "checkout_verify", p.id)
    return {"order": order, "label": TEST_LABEL}


async def handle_webhook(event: dict) -> str:
    etype = event.get("event", "")
    if etype not in ("payment.captured", "payment.authorized", "order.paid"):
        return "ignored"
    payment = (event.get("payload", {}).get("payment") or {}).get("entity") or {}
    order_id, payment_id = payment.get("order_id"), payment.get("id")
    if not order_id or not payment_id:
        return "ignored"
    async with transaction() as conn:
        order = row(await conn.fetchrow(f"select {ORDER_COLS} from payment_orders where razorpay_order_id = $1 for update", order_id))
        if not order:
            return "ignored"
        await _settle(conn, order, payment_id, "webhook", None)
    return "processed"


async def list_orders(user_id: UUID) -> list[dict]:
    async with transaction() as conn:
        return rows(await conn.fetch(f"select {ORDER_COLS} from payment_orders where user_id = $1 order by created_at desc", user_id))
