from fastapi import APIRouter, Depends

from app.auth.deps import Principal, get_principal
from app.domain.schemas import OrderIn, VerifyIn
from app.repositories.db import transaction
from app.services import billing, entitlements

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", summary="Plan catalogue (server-defined entitlements)")
async def plans(_: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await billing.list_plans(conn), "label": billing.TEST_LABEL}


@router.get("/subscription")
async def subscription(p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"subscription": entitlements.summarize(await entitlements.active_subscription(conn, p.id))}


@router.post("/orders", status_code=201, summary="Create a Razorpay TEST order (idempotent) and checkout options")
async def create_order(body: OrderIn, p: Principal = Depends(get_principal)):
    return await billing.create_order(p, body.plan_code, body.idempotency_key)


@router.post("/verify", summary="Verify the checkout signature server-side; grants the plan only after verification")
async def verify(body: VerifyIn, p: Principal = Depends(get_principal)):
    return await billing.verify_checkout(p, body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature)


@router.get("/orders")
async def orders(p: Principal = Depends(get_principal)):
    return {"items": await billing.list_orders(p.id), "label": billing.TEST_LABEL}
