from fastapi import APIRouter

from app.api.v1 import admin, assist, billing, internal, journey, me, staff, webhooks

api_v1 = APIRouter(prefix="/v1")
for r in (me.router, journey.router, assist.router, billing.router, staff.router, admin.router, webhooks.router, internal.router):
    api_v1.include_router(r)
