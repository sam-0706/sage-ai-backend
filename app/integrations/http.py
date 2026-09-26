"""Outbound HTTP with timeouts, bounded retries, correlation IDs and sanitized errors."""
import asyncio
import logging
import random

import httpx

from app.core.context import get_correlation_id
from app.core.errors import ProviderError
from app.core.logging import log

logger = logging.getLogger("sage.http")

_client: httpx.AsyncClient | None = None
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0),
                                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict | None = None,
    json: dict | None = None,
    params: dict | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    retry_non_idempotent: bool = False,
    expected: tuple[int, ...] = (200, 201),
) -> dict | list:
    """Make a JSON request. Retries only idempotent methods unless retry_non_idempotent=True
    (use that only when the provider call is itself idempotent, e.g. keyed by a receipt)."""
    idempotent = method.upper() in {"GET", "HEAD", "PUT", "DELETE"} or retry_non_idempotent
    attempts = 1 + (retries if retries is not None else (2 if idempotent else 0))
    hdrs = {"X-Request-ID": get_correlation_id(), "Accept": "application/json", **(headers or {})}
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await client().request(method, url, headers=hdrs, json=json, params=params, auth=auth,
                                          timeout=timeout or 20.0)
            if resp.status_code in expected:
                return resp.json() if resp.content else {}
            log(logger, logging.WARNING, "provider non-success", provider=provider, status=resp.status_code,
                attempt=attempt, body=resp.text[:500])
            if resp.status_code in RETRYABLE_STATUS and attempt < attempts:
                await asyncio.sleep(min(4.0, 0.4 * 2 ** attempt) + random.random() * 0.2)
                continue
            raise ProviderError(f"{provider} request failed", details={"provider": provider, "status": resp.status_code},
                                status_code=502 if resp.status_code >= 500 else 424)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            log(logger, logging.WARNING, "provider transport error", provider=provider, attempt=attempt, err=type(e).__name__)
            if attempt < attempts:
                await asyncio.sleep(min(4.0, 0.4 * 2 ** attempt) + random.random() * 0.2)
                continue
    raise ProviderError(f"{provider} is unavailable", details={"provider": provider}, status_code=503) from last_exc
