"""Knowledge chatbot, advisor templates and the faculty/advisor directory."""
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.auth.deps import Principal, get_principal
from app.core.errors import AppError
from app.domain.schemas import ChatMessageIn, ChatSessionIn
from app.repositories.db import rows, transaction
from app.services import chat as chat_svc
from app.services import knowledge

router = APIRouter()


@router.get("/advisor-templates", tags=["support"], summary="Labelled human-support templates (not real people)")
async def advisor_templates(mode: str | None = None, p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": rows(await conn.fetch(
            "select key, modes::text[] as modes, category, title, description, help_types, prep_questions, share_checklist, label "
            "from advisor_templates where $1::experience_mode = any(modes) order by sort_order", mode or p.mode))}


@router.get("/directory", tags=["support"], summary="Faculty, advisor and office directory (demo records are labelled)")
async def directory(q: str | None = Query(None, max_length=100), p: Principal = Depends(get_principal)):
    async with transaction() as conn:
        return {"items": await knowledge.search_directory(conn, q, p.institution_id, 30)}


@router.get("/knowledge/search", tags=["support"], summary="Semantic + keyword search over the knowledge base")
async def knowledge_search(q: str = Query(min_length=2, max_length=300), k: int = Query(6, le=12),
                           p: Principal = Depends(get_principal)):
    hits = await knowledge.search(q, k=k, institution_id=p.institution_id, mode=p.mode)
    return {"items": [{**h, "chunk_id": str(h["chunk_id"]), "source_id": str(h["source_id"])} for h in hits]}


@router.post("/chat/sessions", tags=["chat"], status_code=201)
async def create_session(body: ChatSessionIn, p: Principal = Depends(get_principal)):
    return await chat_svc.create_session(p.id, body.title)


@router.get("/chat/sessions", tags=["chat"])
async def list_sessions(p: Principal = Depends(get_principal)):
    return {"items": await chat_svc.list_sessions(p.id)}


@router.get("/chat/sessions/{session_id}/messages", tags=["chat"])
async def list_messages(session_id: UUID, p: Principal = Depends(get_principal)):
    return {"items": await chat_svc.get_messages(p.id, session_id)}


@router.delete("/chat/sessions/{session_id}", tags=["chat"])
async def delete_session(session_id: UUID, p: Principal = Depends(get_principal)):
    await chat_svc.delete_session(p.id, session_id)
    return {"deleted": True}


@router.post("/chat/sessions/{session_id}/messages", tags=["chat"],
             summary="Ask the assistant. stream=true returns Server-Sent Events (meta, delta, done, error); "
                     "stream=false returns one JSON object.")
async def send_message(session_id: UUID, body: ChatMessageIn, p: Principal = Depends(get_principal)):
    gen = chat_svc.send_message(p, session_id, body.content)
    first = await anext(gen)  # surface permission/quota errors as normal HTTP errors before streaming starts

    if body.stream:
        async def sse():
            for ev in (first,):
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            async for ev in gen:
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    text, meta, done = [], first if first["type"] == "meta" else {}, {}
    if first["type"] == "delta":
        text.append(first["text"])
    if first["type"] == "error":
        raise AppError(first["message"], code=first["code"], status_code=502)
    async for ev in gen:
        if ev["type"] == "delta":
            text.append(ev["text"])
        elif ev["type"] == "meta":
            meta = ev
        elif ev["type"] == "done":
            done = ev
        elif ev["type"] == "error":
            raise AppError(ev["message"], code=ev["code"], status_code=502)
    return {"message_id": done.get("message_id"), "content": "".join(text), "citations": meta.get("citations", []),
            "safety": meta.get("safety", {})}
