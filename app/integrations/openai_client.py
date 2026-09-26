"""One OpenAI provider service: structured outputs, timeouts, bounded retries, prompt versioning, cost logging.

Every call writes an `ai_runs` row with prompt version, model, request id, latency, token usage and
validation outcome (PRD: Model Output Contract).
"""
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar
from uuid import UUID

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.context import get_correlation_id
from app.core.errors import ProviderError
from app.core.logging import log
from app.repositories.db import connection

logger = logging.getLogger("sage.ai")
T = TypeVar("T", bound=BaseModel)


@lru_cache
def _client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.openai_api_key, timeout=s.openai_timeout_s, max_retries=s.openai_max_retries)


class _InvalidOutput(Exception):
    pass


@dataclass
class AIResult[T]:
    data: T
    run_id: UUID
    model: str


async def _log_run(*, user_id, task, prompt_version, model, request_id, latency_ms, usage, status,
                   validation_ok, error=None) -> UUID:
    async with connection() as conn:
        return await conn.fetchval(
            """insert into ai_runs (user_id, task, prompt_version, model, provider_request_id, correlation_id, latency_ms,
                                    input_tokens, output_tokens, status, validation_ok, error)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) returning id""",
            user_id, task, prompt_version, model, request_id, get_correlation_id(), latency_ms,
            getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None),
            getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None),
            status, validation_ok, (error or "")[:500] or None)


def _reasoning_kwargs(model: str, effort: str) -> dict:
    # GPT-5 family / o-series are reasoning models: no temperature, support reasoning_effort.
    if model.startswith(("gpt-5", "o")):
        return {"reasoning_effort": effort}
    return {"temperature": 0.3}


async def structured(
    *,
    task: str,
    prompt_version: str,
    system: str,
    user: str,
    schema: type[T],
    user_id: UUID | None,
    model: str | None = None,
    effort: str = "low",
) -> AIResult[T]:
    """Run a structured-output completion and validate against `schema`. Raises ProviderError on failure."""
    s = get_settings()
    model = model or s.openai_model
    started = time.perf_counter()
    request_id = usage = None
    try:
        completion = await _client().chat.completions.parse(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            **_reasoning_kwargs(model, effort),
        )
        request_id = getattr(completion, "_request_id", None) or completion.id
        usage = completion.usage
        msg = completion.choices[0].message
        if msg.refusal:
            raise ProviderError("The AI model declined this request", code="ai_refusal")
        parsed = msg.parsed
        if parsed is None:
            raise _InvalidOutput("model returned no parseable output")
        latency = int((time.perf_counter() - started) * 1000)
        run_id = await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model,
                                request_id=request_id, latency_ms=latency, usage=usage, status="succeeded", validation_ok=True)
        return AIResult(data=parsed, run_id=run_id, model=model)
    except (openai.APIError, ValidationError, ProviderError, json.JSONDecodeError, _InvalidOutput) as e:
        latency = int((time.perf_counter() - started) * 1000)
        await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model, request_id=request_id,
                       latency_ms=latency, usage=usage, status="failed",
                       validation_ok=False if isinstance(e, ValidationError | _InvalidOutput | json.JSONDecodeError) else None, error=f"{type(e).__name__}: {e}")
        log(logger, logging.WARNING, "ai task failed", task=task, err=type(e).__name__)
        if isinstance(e, ProviderError):
            raise
        raise ProviderError("The AI service is temporarily unavailable", code="ai_unavailable") from e


async def web_search_structured(
    *,
    task: str,
    prompt_version: str,
    instructions: str,
    input_text: str,
    schema: type[T],
    user_id: UUID | None,
    model: str | None = None,
) -> AIResult[T]:
    """Use the Responses web-search tool and validate the final answer against `schema`."""
    s = get_settings()
    model = model or s.openai_model
    started = time.perf_counter()
    request_id = usage = None
    try:
        response = await _client().responses.parse(
            model=model,
            instructions=instructions,
            input=input_text,
            tools=[{
                "type": "web_search",
                "external_web_access": True,
                "search_context_size": "high",
                "user_location": {"type": "approximate", "city": "Mumbai", "region": "Maharashtra", "country": "IN", "timezone": "Asia/Kolkata"},
            }],
            text_format=schema,
            reasoning={"effort": "low"},
            max_tool_calls=10,
        )
        request_id = getattr(response, "_request_id", None) or response.id
        usage = response.usage
        parsed = response.output_parsed
        if parsed is None:
            raise _InvalidOutput("model returned no parseable web-search output")
        latency = int((time.perf_counter() - started) * 1000)
        run_id = await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model,
                                request_id=request_id, latency_ms=latency, usage=usage, status="succeeded", validation_ok=True)
        return AIResult(data=parsed, run_id=run_id, model=model)
    except (openai.APIError, ValidationError, json.JSONDecodeError, _InvalidOutput) as e:
        latency = int((time.perf_counter() - started) * 1000)
        await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model, request_id=request_id,
                       latency_ms=latency, usage=usage, status="failed", validation_ok=False, error=f"{type(e).__name__}: {e}")
        log(logger, logging.WARNING, "web search task failed", task=task, err=type(e).__name__)
        raise ProviderError("Live job search is temporarily unavailable", code="job_search_unavailable") from e


async def stream_text(
    *,
    task: str,
    prompt_version: str,
    messages: list[dict],
    user_id: UUID | None,
    model: str | None = None,
    effort: str = "low",
    on_complete=None,
) -> AsyncIterator[str]:
    """Stream text deltas. Logs the run when the stream finishes; `on_complete(text, run_id)` is awaited at the end."""
    s = get_settings()
    model = model or s.chat_model
    started = time.perf_counter()
    parts: list[str] = []
    usage = request_id = None
    try:
        stream = await _client().chat.completions.create(
            model=model, messages=messages, stream=True, stream_options={"include_usage": True},
            **_reasoning_kwargs(model, effort),
        )
        async for chunk in stream:
            request_id = request_id or chunk.id
            if chunk.usage:
                usage = chunk.usage
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                delta = chunk.choices[0].delta.content
                parts.append(delta)
                yield delta
    except openai.APIError as e:
        await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model, request_id=request_id,
                       latency_ms=int((time.perf_counter() - started) * 1000), usage=usage, status="failed",
                       validation_ok=None, error=f"{type(e).__name__}: {e}")
        raise ProviderError("The AI service is temporarily unavailable", code="ai_unavailable") from e
    run_id = await _log_run(user_id=user_id, task=task, prompt_version=prompt_version, model=model, request_id=request_id,
                            latency_ms=int((time.perf_counter() - started) * 1000), usage=usage, status="succeeded",
                            validation_ok=True)
    if on_complete:
        await on_complete("".join(parts), run_id)


async def complete_text(*, task: str, prompt_version: str, messages: list[dict], user_id: UUID | None,
                        model: str | None = None, effort: str = "low") -> tuple[str, UUID]:
    out: list[str] = []
    holder: dict = {}

    async def _done(text, run_id):
        holder["run_id"] = run_id

    async for d in stream_text(task=task, prompt_version=prompt_version, messages=messages, user_id=user_id,
                               model=model, effort=effort, on_complete=_done):
        out.append(d)
    return "".join(out), holder["run_id"]


async def embed(texts: list[str]) -> list[list[float]]:
    s = get_settings()
    try:
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            resp = await _client().embeddings.create(model=s.openai_embedding_model, input=texts[i:i + 96])
            out.extend(d.embedding for d in resp.data)
        return out
    except openai.APIError as e:
        raise ProviderError("The embedding service is temporarily unavailable", code="ai_unavailable") from e


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
