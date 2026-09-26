"""College knowledge base for RAG: ingestion, chunking, embeddings (pgvector), hybrid retrieval.

Sources are managed by superadmins. Retrieved text is treated as untrusted content when prompting.
"""
import hashlib
import html
import io
import ipaddress
import re
import socket
from urllib.parse import urlparse
from uuid import UUID

import asyncpg

from app.core.errors import AppError, NotFound, ProviderError
from app.integrations import openai_client
from app.integrations.http import client as http_client
from app.repositories import audit
from app.repositories.db import row, rows, transaction

CHUNK_CHARS = 1800
OVERLAP_CHARS = 250
MAX_SOURCE_CHARS = 400_000

SOURCE_COLS = """id, institution_id, title, kind, url, category, audience::text[] as audience, status, chunk_count, is_demo,
                 error, created_at, updated_at"""


def chunk_text(text: str) -> list[tuple[str | None, str]]:
    """Split on markdown-style headings, then pack paragraphs into ~CHUNK_CHARS windows with overlap."""
    text = re.sub(r"\r\n?", "\n", text).strip()
    sections: list[tuple[str | None, str]] = []
    heading, buf = None, []
    for line in text.split("\n"):
        m = re.match(r"^#{1,4}\s+(.*)", line)
        if m:
            if "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))

    chunks: list[tuple[str | None, str]] = []
    for h, body in sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        cur = ""
        for p in paras:
            while len(p) > CHUNK_CHARS:  # very long paragraph: hard split
                if cur:
                    chunks.append((h, cur))
                    cur = ""
                chunks.append((h, p[:CHUNK_CHARS]))
                p = p[CHUNK_CHARS - OVERLAP_CHARS:]
            if len(cur) + len(p) + 2 > CHUNK_CHARS and cur:
                chunks.append((h, cur))
                cur = cur[-OVERLAP_CHARS:] + "\n\n" + p
            else:
                cur = f"{cur}\n\n{p}" if cur else p
        if cur:
            chunks.append((h, cur))
    return chunks


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<h([1-4])[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|br|section|article)>|<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages)


def _assert_public_url(url: str) -> None:
    """SSRF guard: only http(s) to public addresses."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise AppError("Only public http(s) URLs can be ingested", code="invalid_url")
    try:
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise AppError("URL resolves to a non-public address", code="invalid_url")
    except socket.gaierror as e:
        raise AppError("URL host could not be resolved", code="invalid_url") from e


async def fetch_url_text(url: str) -> str:
    _assert_public_url(url)
    try:
        resp = await http_client().get(url, timeout=20.0, follow_redirects=True,
                                       headers={"User-Agent": "SAGE-AI-KnowledgeBot/1.0"})
    except Exception as e:
        raise ProviderError("Could not fetch the URL", code="fetch_failed") from e
    if resp.status_code != 200:
        raise ProviderError(f"URL returned HTTP {resp.status_code}", code="fetch_failed")
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype:
        return pdf_to_text(resp.content)
    return html_to_text(resp.text) if "html" in ctype else resp.text


async def ingest(*, title: str, kind: str, text: str, url: str | None = None, category: str | None = None,
                 audience: list[str] | None = None, institution_id: UUID | None = None, is_demo: bool = False,
                 created_by: UUID | None = None, replace_source_id: UUID | None = None) -> dict:
    text = (text or "").strip()[:MAX_SOURCE_CHARS]
    if len(text) < 40:
        raise AppError("Source has too little text to index", code="empty_source")
    checksum = hashlib.sha256(text.encode()).hexdigest()
    audience = audience or ["student", "professional", "founder"]
    async with transaction() as conn:
        dup = await conn.fetchval("select id from knowledge_sources where checksum = $1 and status = 'ready' and "
                                  "institution_id is not distinct from $2::uuid", checksum, institution_id)
        if dup and dup != replace_source_id:
            raise AppError("An identical source is already indexed", code="duplicate_source", details={"source_id": str(dup)})
        if replace_source_id:
            source_id = replace_source_id
            await conn.execute("update knowledge_sources set status = 'pending', error = null, title = $2, checksum = $3 "
                               "where id = $1", source_id, title, checksum)
        else:
            source_id = await conn.fetchval(
                """insert into knowledge_sources (institution_id, title, kind, url, category, audience, checksum, is_demo, created_by)
                   values ($1,$2,$3,$4,$5,$6::experience_mode[],$7,$8,$9) returning id""",
                institution_id, title, kind, url, category, audience, checksum, is_demo, created_by)

    chunks = chunk_text(text)
    try:
        vectors = await openai_client.embed([f"{title}\n{h or ''}\n{c}" for h, c in chunks])
    except ProviderError as e:
        async with transaction() as conn:
            await conn.execute("update knowledge_sources set status = 'failed', error = $2 where id = $1", source_id, e.message)
        raise

    async with transaction() as conn:
        await conn.execute("delete from knowledge_chunks where source_id = $1", source_id)
        await conn.executemany(
            "insert into knowledge_chunks (source_id, chunk_index, heading, content, embedding) values ($1,$2,$3,$4,$5::vector)",
            [(source_id, i, h, c, openai_client.to_pgvector(v)) for i, ((h, c), v) in enumerate(zip(chunks, vectors, strict=True))])
        await conn.execute("update knowledge_sources set status = 'ready', chunk_count = $2 where id = $1", source_id, len(chunks))
        await audit.record(conn, "knowledge.indexed", actor_id=created_by, target_type="knowledge_source", target_id=source_id,
                           metadata={"chunks": len(chunks), "kind": kind})
        return row(await conn.fetchrow(f"select {SOURCE_COLS} from knowledge_sources where id = $1", source_id))


async def search(query: str, *, k: int = 6, institution_id: UUID | None = None, mode: str | None = None) -> list[dict]:
    [vec] = await openai_client.embed([query])
    async with transaction() as conn:
        return rows(await conn.fetch(
            "select * from match_knowledge($1::vector, $2, $3, $4, $5::experience_mode)",
            openai_client.to_pgvector(vec), query, k, institution_id, mode))


async def list_sources(conn: asyncpg.Connection) -> list[dict]:
    return rows(await conn.fetch(f"select {SOURCE_COLS} from knowledge_sources order by created_at desc"))


async def delete_source(conn: asyncpg.Connection, source_id: UUID, actor: UUID) -> None:
    n = await conn.execute("delete from knowledge_sources where id = $1", source_id)
    if n.endswith(" 0"):
        raise NotFound("Source not found")
    await audit.record(conn, "knowledge.deleted", actor_id=actor, target_type="knowledge_source", target_id=source_id)


# ------------------------------------------------------------------ faculty / advisor directory
async def search_directory(conn: asyncpg.Connection, q: str | None, institution_id: UUID | None, limit: int = 20) -> list[dict]:
    args: list = [institution_id]
    where = "(institution_id is null or $1::uuid is null or institution_id = $1::uuid or is_demo)"
    if q:
        args.append(q)
        where += f""" and (name ilike '%'||${len(args)}||'%' or department ilike '%'||${len(args)}||'%'
                          or exists (select 1 from unnest(subjects || expertise) x where x ilike '%'||${len(args)}||'%')
                          or similarity(coalesce(bio,''), ${len(args)}) > 0.1)"""
    args.append(limit)
    return rows(await conn.fetch(
        f"""select id, name, kind, department, designation, subjects, expertise, office_hours, contact_hint, bio, is_demo, label
            from faculty_directory where {where} order by is_demo, name limit ${len(args)}""", *args))
