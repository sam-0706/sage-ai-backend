"""SAGE AI API — one versioned backend for the web, Electron desktop and Flutter mobile clients."""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.device_page import router as device_page_router
from app.api.health import router as health_router
from app.api.v1.router import api_v1
from app.core.config import get_settings
from app.core.context import correlation_id_var, new_correlation_id
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, log
from app.integrations.http import close_client
from app.repositories.db import close_pool

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("sage.http.access")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # No schema creation at startup (PRD): migrations are applied explicitly via scripts/migrate.py.
    yield
    await close_client()
    await close_pool()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "SAGE AI (Strategic Action and Growth Engine) API. Authenticate with `Authorization: Bearer <Clerk session token>`. "
        "Errors always use the envelope `{error: {code, message, details, correlation_id}}`."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
install_error_handlers(app)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"^https://([a-z0-9-]+\.)*vercel\.app$|^(app|file|capacitor|tauri)://.*$",
    allow_credentials=False,  # bearer tokens, not cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    cid = request.headers.get("x-request-id") or new_correlation_id()
    token = correlation_id_var.set(cid[:64])
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        correlation_id_var.reset(token)
    response.headers["X-Request-ID"] = cid[:64]
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/v1/"):
        response.headers.setdefault("Cache-Control", "no-store")
    correlation_id_var.set(cid[:64])
    log(logger, logging.INFO, "request", method=request.method, path=request.url.path, status=response.status_code,
        ms=int((time.perf_counter() - started) * 1000))
    return response


@app.get("/", include_in_schema=False)
async def root():
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs", "api": "/v1", "health": "/health/ready"}


app.include_router(health_router)
app.include_router(device_page_router)
app.include_router(api_v1)
