from fastapi import APIRouter

from app.api.v1 import admin, assist, auth, autoapply, billing, examprep, internal, journey, me, onboarding, staff, webhooks

api_v1 = APIRouter(prefix="/v1")
for r in (auth.router, onboarding.router, examprep.router, autoapply.router, me.router, journey.router, assist.router, billing.router, staff.router, admin.router, webhooks.router, internal.router):
    api_v1.include_router(r)
