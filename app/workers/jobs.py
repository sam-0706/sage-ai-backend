"""Durable background work, driven by cron and on-read triggers (serverless has no long-lived worker):
- reconcile open voice calls against the provider
- retry pending/failed AI extractions (bounded)
- expire lapsed subscriptions and stale payment orders
All jobs are idempotent and safe to run concurrently with webhooks.
"""
from app.repositories.db import transaction
from app.services import calls as calls_svc


async def run_maintenance() -> dict:
    from app.services.campus import check_reminders
    reminders = await check_reminders()
    result = await calls_svc.reconcile_open_calls(limit=50)
    async with transaction() as conn:
        # retry failed extractions at most 3 attempts
        retry = await conn.execute(
            """update calls c set extraction_status = 'pending'
               where c.status = 'completed' and c.extraction_status = 'failed'
                 and (select count(*) from extractions e where e.call_id = c.id) < 3""")
        expired = await conn.execute("update subscriptions set status = 'expired' where status = 'active' and period_end < now()")
        stale_orders = await conn.execute("update payment_orders set status = 'expired' where status = 'created' "
                                          "and created_at < now() - interval '2 days'")
    result.update({"extractions_requeued": int(retry.split()[-1]), "subscriptions_expired": int(expired.split()[-1]),
                   "orders_expired": int(stale_orders.split()[-1])})
    result["deadline_reminders"] = reminders
    return result
