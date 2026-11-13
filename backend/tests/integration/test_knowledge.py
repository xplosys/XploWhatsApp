"""Knowledge base: uploads through storage and the virus scanner, extraction, chunking,
embeddings, versions, web pages with SSRF checks, retrieval in automatic replies and
the preview, the search test, re-indexing after a model change and the sweeper.

Real PostgreSQL (pgvector), Redis and S3-compatible storage; fake AI providers, a fake
clamd and a fake web."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.db.session import admin_transaction, app_transaction, drain_after_commit
from app.modules.ai.models import AIModel, ModelKind
from app.modules.knowledge.models import KnowledgeChunk, KnowledgeEmbedding
from app.workers.dispatch import DISPATCHED, KNOWLEDGE_PROCESS, KNOWLEDGE_REINDEX
from tests.conftest import Actor, RegisterFn, token_from_outbox
from tests.integration.test_ai import (  # noqa: F401  (fixtures used by name)
    ai,
    ai_messages,
    config,
    deliver,
    enable_provider,
    meta,
    models,
    plan_limits,
    run_jobs,
    setup_assistant,
    usage,
)
from tests.integration.test_whatsapp import connect
from tests.support.ai import FakeAI
from tests.support.clamd import EICAR, FakeClamd
from tests.support.files import make_docx, make_macro_xlsx, make_pdf, make_xlsx, make_zip_bomb_docx
from tests.support.meta import FakeMeta, inbound_text

API = "/api/v1"
K = f"{API}/knowledge"

PRICES = (
    "Teeth whitening\n"
    "Professional teeth whitening takes 60 minutes and costs ₹6,000.\n\n"
    "Dental implants\n"
    "A single implant starts at ₹28,000 including the crown."
)


@pytest.fixture
async def embedding_models(
    models: dict[str, uuid.UUID],
) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Embedding models next to the chat ones. Models are never deleted in production
    (only switched off), so knowledge rows that point at them go first on teardown."""
    ids: dict[str, uuid.UUID] = {}
    async with admin_transaction() as s:
        for provider, model_id in (("openai", "text-embedding-test"), ("gemini", "gemini-embedding-test")):
            m = AIModel(
                provider=provider, model_id=model_id, active=True, capabilities={}, kind=ModelKind.EMBEDDING
            )
            s.add(m)
            await s.flush()
            ids[provider] = m.id
    yield ids
    import asyncpg

    from tests.conftest import superuser_dsn

    conn = await asyncpg.connect(superuser_dsn())
    try:
        await conn.execute("TRUNCATE knowledge_settings, knowledge_chunk_embeddings")
    finally:
        await conn.close()


@pytest.fixture
def scanner(clamd: FakeClamd) -> Iterator[FakeClamd]:
    clamd.broken = False
    clamd.scanned.clear()
    yield clamd
    clamd.broken = False


class FakeWeb:
    """Pages by hostname, and a DNS answer per hostname."""

    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], httpx.Response] = {}
        self.dns: dict[str, list[str]] = {}
        self.requests: list[httpx.Request] = []

    def page(self, host: str, path: str, body: str | bytes, content_type: str = "text/html") -> None:
        content = body.encode() if isinstance(body, str) else body
        self.pages[(host, path)] = httpx.Response(
            200, content=content, headers={"content-type": content_type}
        )

    def redirect(self, host: str, path: str, location: str) -> None:
        self.pages[(host, path)] = httpx.Response(302, headers={"location": location})

    async def resolve(self, host: str, port: int) -> list[str]:
        from app.modules.knowledge.webfetch import FetchError

        if host in self.dns:
            return self.dns[host]
        if host.replace(".", "").isdigit() or ":" in host:
            return [host]
        raise FetchError(f"The address {host} could not be found.", "dns_failed")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.headers["host"].split(":")[0]
        response = self.pages.get((host, request.url.path))
        if response is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWeb]:
    from app.modules.knowledge import webfetch

    fake = FakeWeb()
    monkeypatch.setattr(webfetch, "resolve", fake.resolve)
    webfetch.set_transport_override(httpx.MockTransport(fake.handler))
    yield fake
    webfetch.set_transport_override(None)


async def run_all() -> list[str]:
    """Run queued jobs, including knowledge ones, until none are left."""
    from app.modules.knowledge.service import process_version, reindex

    results: list[str] = []
    await drain_after_commit()
    while DISPATCHED:
        job = DISPATCHED[0]
        if job.task == KNOWLEDGE_PROCESS:
            DISPATCHED.pop(0)
            results.append(await process_version(uuid.UUID(job.args[0]), uuid.UUID(job.args[1])))
        elif job.task == KNOWLEDGE_REINDEX:
            DISPATCHED.pop(0)
            results.append(await reindex(uuid.UUID(job.args[0])))
        else:
            results += await run_jobs()
            continue
        await drain_after_commit()
    return results


@pytest.fixture
async def owner(
    register: RegisterFn,
    ai: FakeAI,
    embedding_models: dict[str, uuid.UUID],
    scanner: FakeClamd,
) -> Actor:
    actor = await register("owner@sunrisedental.in", "Sunrise Dental")
    assert (await enable_provider(actor, "openai")).status_code == 200
    r = await actor.put(
        f"{K}/settings", json={"embedding_provider": "openai", "embedding_model": "text-embedding-test"}
    )
    assert r.status_code == 200, r.text
    return actor


async def upload(
    actor: Actor, filename: str, data: bytes, title: str | None = None, *, run: bool = True
) -> httpx.Response:
    form = {"title": title} if title else {}
    r = await actor.post(f"{K}/documents/upload", files={"file": (filename, data)}, data=form)
    if run and r.status_code == 201:
        await run_all()
    return r


async def document(actor: Actor, document_id: str) -> dict[str, Any]:
    r = await actor.get(f"{K}/documents/{document_id}")
    assert r.status_code == 200, r.text
    return dict(r.json())


async def chunk_count(tenant_id: str) -> int:
    async with app_transaction(uuid.UUID(tenant_id)) as s:
        return int(await s.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)


async def embedding_model_pks(tenant_id: str) -> set[uuid.UUID]:
    async with app_transaction(uuid.UUID(tenant_id)) as s:
        return set((await s.execute(select(KnowledgeEmbedding.model_pk).distinct())).scalars())


# ---- settings -------------------------------------------------------------------------


async def test_an_embedding_model_is_chosen_before_adding_documents(
    register: RegisterFn,
    ai: FakeAI,
    embedding_models: dict[str, uuid.UUID],
) -> None:
    actor = await register("owner@acme.in")
    settings = (await actor.get(f"{K}/settings")).json()
    assert settings["embedding_model"] is None and settings["storage_ready"] is True
    assert settings["virus_scan"] is True and settings["usage"]["max_documents"] == 20
    assert settings["min_score"] == "0.300" and settings["top_k"] == 5

    r = await upload(actor, "prices.txt", PRICES.encode())
    assert r.status_code == 409 and r.json()["error"]["code"] == "embedding_model_required"

    offered = (await actor.get(f"{K}/embedding-models")).json()
    assert {(m["provider"], m["model_id"]) for m in offered} == {
        ("openai", "text-embedding-test"),
        ("gemini", "gemini-embedding-test"),
    }
    r = await actor.put(
        f"{K}/settings", json={"embedding_provider": "openai", "embedding_model": "gpt-test-mini"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_unavailable"
    r = await actor.put(f"{K}/settings", json={"embedding_model": "text-embedding-test"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_incomplete"
    r = await actor.put(f"{K}/settings", json={"min_score": "1.5"})
    assert r.status_code == 422

    r = await actor.put(
        f"{K}/settings",
        json={"embedding_provider": "openai", "embedding_model": "text-embedding-test", "top_k": 3},
    )
    assert r.status_code == 200
    assert r.json()["embedding_model"]["model_id"] == "text-embedding-test" and r.json()["top_k"] == 3

    # Without a usable key the document fails with a clear reason.
    r = await upload(actor, "prices.txt", PRICES.encode())
    assert r.status_code == 201
    latest = (await document(actor, r.json()["id"]))["latest"]
    assert latest["status"] == "failed" and latest["error_code"] == "embedding_key_missing"

    # Adding the key and retrying makes it ready.
    await enable_provider(actor, "openai")
    r = await actor.post(f"{K}/documents/{r.json()['id']}/versions/1/retry")
    assert r.status_code == 200 and r.json()["latest"]["status"] == "queued"
    await run_all()
    assert (await document(actor, r.json()["id"]))["latest"]["status"] == "ready"


async def test_knowledge_needs_manage_permission(
    owner: Actor,
    register: RegisterFn,
    ai: FakeAI,
) -> None:
    await owner.post(
        f"{API}/tenants/current/invitations", json={"email": "agent@sunrisedental.in", "role": "agent"}
    )
    agent = await register(
        "agent@sunrisedental.in", business=None, invitation_token=token_from_outbox("/accept-invite?token=")
    )
    assert (await agent.get(f"{K}/documents")).status_code == 200
    assert (await agent.get(f"{K}/settings")).status_code == 200
    assert (await upload(agent, "prices.txt", PRICES.encode())).status_code == 403
    assert (await agent.post(f"{K}/documents/text", json={"title": "x", "text": "y"})).status_code == 403
    assert (await agent.put(f"{K}/settings", json={"top_k": 2})).status_code == 403
    assert (await agent.post(f"{K}/search", json={"query": "price"})).status_code == 403


# ---- files ----------------------------------------------------------------------------


async def test_every_supported_format_becomes_chunks(owner: Actor, ai: FakeAI, scanner: FakeClamd) -> None:
    files = {
        "prices.txt": PRICES.encode(),
        "brochure.pdf": make_pdf(
            ["Clinic brochure", "Root canal treatment from Rs. 4,500."], title="Brochure"
        ),
        "handbook.docx": make_docx(),
        "price-list.xlsx": make_xlsx(),
        "extras.csv": "Service,Price\nFluoride varnish,₹700\nNight guard,₹3500\n".encode(),
        "faq.md": b"# FAQ\n\n## Parking\nFree parking behind the clinic.\n",
    }
    ids = {}
    for name, data in files.items():
        r = await upload(owner, name, data, run=False)
        assert r.status_code == 201, (name, r.text)
        assert r.json()["latest"]["status"] == "queued" and r.json()["source_type"] == "file"
        ids[name] = r.json()["id"]
    results = await run_all()
    assert results == ["ready"] * len(files)
    assert len(scanner.scanned) == len(files)

    for name, doc_id in ids.items():
        doc = await document(owner, doc_id)
        latest = doc["latest"]
        assert latest["status"] == "ready" and latest["scan_result"] == "clean", name
        assert latest["chunk_count"] >= 1 and latest["char_count"] > 0 and doc["active_version"] == 1

    pdf_chunks = (await owner.get(f"{K}/documents/{ids['brochure.pdf']}/chunks")).json()
    assert "Rs. 4,500" in pdf_chunks[0]["content"] and pdf_chunks[0]["meta"]["pages"] == [1, 2]
    docx_chunks = (await owner.get(f"{K}/documents/{ids['handbook.docx']}/chunks")).json()
    whitening = next(c for c in docx_chunks if "whitening" in c["content"])
    # Small sections share a chunk; each keeps its own heading inline.
    assert "Treatments > Teeth whitening\nProfessional teeth whitening" in whitening["content"]
    assert any("Dental X-ray | ₹350" in c["content"] for c in docx_chunks)
    xlsx_chunks = (await owner.get(f"{K}/documents/{ids['price-list.xlsx']}/chunks")).json()
    assert "Service: Crown; Price (₹): 8000; Duration: 2 visits" in xlsx_chunks[0]["content"]
    assert xlsx_chunks[0]["meta"]["sheet"] == "Price list"
    csv_chunks = (await owner.get(f"{K}/documents/{ids['extras.csv']}/chunks")).json()
    assert "Service: Night guard; Price: ₹3500" in csv_chunks[0]["content"]

    # Every chunk has a vector from the chosen model, and the calls were recorded.
    assert len(ai.embed_calls("openai")) == len(files)
    assert all(call["dimensions"] == 1536 for call in ai.embed_calls("openai"))
    records = [r for r in await usage(owner.tenant_id) if r.dimensions.get("purpose") == "knowledge_index"]
    assert len(records) == len(files) and all(r.metric == "ai_call" for r in records)

    settings = (await owner.get(f"{K}/settings")).json()
    assert settings["usage"]["documents"] == len(files)
    assert settings["usage"]["storage_bytes"] == sum(len(d) for d in files.values())
    listed = (await owner.get(f"{K}/documents")).json()
    assert {d["title"] for d in listed} == {"prices", "brochure", "handbook", "price-list", "extras", "faq"}


async def test_download_is_a_short_lived_attachment_link(owner: Actor) -> None:
    data = PRICES.encode()
    r = await upload(owner, "prices.txt", data)
    r = await owner.get(f"{K}/documents/{r.json()['id']}/versions/1/download")
    assert r.status_code == 200 and r.json()["expires_in"] == 300
    async with httpx.AsyncClient() as plain:
        stored = await plain.get(r.json()["url"])
    assert stored.status_code == 200 and stored.content == data
    assert stored.headers["content-disposition"] == 'attachment; filename="prices.txt"'
    assert stored.headers["content-type"] == "application/octet-stream"
    async with httpx.AsyncClient() as plain:
        tampered = await plain.get(r.json()["url"].replace("prices", "other"))
    assert tampered.status_code in (403, 404)


async def test_unsafe_or_unsupported_files_are_refused(owner: Actor, scanner: FakeClamd) -> None:
    cases = {
        "setup.exe": b"MZ\x90\x00" + b"\0" * 100,
        "prices.docx": make_pdf(["not really a word file"]),
        "macros.xlsx": make_macro_xlsx(),
        "bomb.docx": make_zip_bomb_docx(),
        "old.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100,
        "image.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 100,
    }
    for name, data in cases.items():
        r = await upload(owner, name, data)
        assert r.status_code == 422, (name, r.text)
        assert r.json()["error"]["code"] == "unsupported_file", name
    assert (await owner.get(f"{K}/documents")).json() == [] and scanner.scanned == []

    # The scanner finds malware: rejected, and the stored file is deleted.
    r = await upload(owner, "notes.txt", EICAR)
    assert r.status_code == 201
    doc = await document(owner, r.json()["id"])
    assert doc["latest"]["status"] == "rejected" and doc["latest"]["error_code"] == "malware_found"
    assert "Eicar" in doc["latest"]["scan_result"] and doc["latest"]["downloadable"] is False
    assert doc["active_version"] is None
    r = await owner.get(f"{K}/documents/{doc['id']}/versions/1/download")
    assert r.status_code == 404
    r = await owner.post(f"{K}/documents/{doc['id']}/versions/1/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_retryable"

    # The scanner is down: the version waits for a retry instead of skipping the scan.
    scanner.broken = True
    r = await upload(owner, "prices.txt", PRICES.encode())
    doc = await document(owner, r.json()["id"])
    assert doc["latest"]["status"] == "failed" and doc["latest"]["error_code"] == "scanner_unavailable"
    assert await chunk_count(owner.tenant_id) == 0
    scanner.broken = False
    await owner.post(f"{K}/documents/{doc['id']}/versions/1/retry")
    await run_all()
    assert (await document(owner, doc["id"]))["latest"]["status"] == "ready"


async def test_scanned_pdf_and_empty_text_explain_why(owner: Actor) -> None:
    r = await upload(owner, "scan.pdf", make_pdf(["", ""]))
    latest = (await document(owner, r.json()["id"]))["latest"]
    assert latest["status"] == "failed" and latest["error_code"] == "no_text" and "OCR" in latest["error"]


async def test_upload_body_size_is_limited(owner: Actor) -> None:
    big = b"a" * (22 * 1024 * 1024)
    r = await owner.post(f"{K}/documents/upload", files={"file": ("big.txt", big)})
    assert r.status_code == 413
    r = await owner.post(f"{K}/documents/upload", files={"file": ("big.txt", big[: 20 * 1024 * 1024 + 10])})
    assert r.status_code == 422 and r.json()["error"]["code"] == "file_too_large"


async def test_plan_limits_documents_and_storage(owner: Actor, plan_limits: Any) -> None:
    await plan_limits(max_knowledge_documents=1)
    assert (await upload(owner, "a.txt", b"Free parking behind the clinic.")).status_code == 201
    r = await upload(owner, "b.txt", b"Open on Saturdays.")
    assert r.status_code == 409 and r.json()["error"]["code"] == "plan_limit_reached"
    r = await owner.post(f"{K}/documents/text", json={"title": "Note", "text": "Hi"})
    assert r.status_code == 409

    await plan_limits(max_knowledge_documents=10, max_storage_mb=0)
    r = await upload(owner, "c.txt", b"Open on Saturdays.")
    assert r.status_code == 409 and "storage" in r.json()["error"]["message"]


# ---- versions -------------------------------------------------------------------------


async def test_new_versions_replace_old_chunks(owner: Actor) -> None:
    r = await upload(owner, "prices.txt", PRICES.encode())
    doc_id = r.json()["id"]
    first_chunks = await chunk_count(owner.tenant_id)
    assert first_chunks >= 1

    r = await owner.post(f"{K}/documents/{doc_id}/versions", files={"file": ("prices.txt", PRICES.encode())})
    assert r.status_code == 409 and r.json()["error"]["code"] == "unchanged"

    updated = PRICES.replace("₹6,000", "₹6,500")
    r = await owner.post(
        f"{K}/documents/{doc_id}/versions", files={"file": ("prices-2026.txt", updated.encode())}
    )
    assert r.status_code == 201
    await run_all()
    doc = await document(owner, doc_id)
    assert doc["active_version"] == 2
    assert [(v["version"], v["status"]) for v in doc["versions"]] == [(2, "ready"), (1, "replaced")]
    assert doc["versions"][1]["chunk_count"] == 0 and doc["versions"][1]["downloadable"] is True
    chunks = (await owner.get(f"{K}/documents/{doc_id}/chunks")).json()
    assert any("₹6,500" in c["content"] for c in chunks)
    assert not any("₹6,000" in c["content"] for c in chunks)

    r = await owner.delete(f"{K}/documents/{doc_id}/versions/2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "active_version"
    r = await owner.delete(f"{K}/documents/{doc_id}/versions/1")
    assert r.status_code == 200 and [v["version"] for v in r.json()["versions"]] == [2]
    settings = (await owner.get(f"{K}/settings")).json()
    assert settings["usage"]["storage_bytes"] == len(updated.encode())

    # Only files are replaced with files.
    note = (await owner.post(f"{K}/documents/text", json={"title": "Note", "text": "Hello"})).json()
    r = await owner.post(f"{K}/documents/{note['id']}/versions", files={"file": ("x.txt", b"other")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "wrong_source"


async def test_notes_are_edited_as_new_versions(owner: Actor) -> None:
    r = await owner.post(
        f"{K}/documents/text", json={"title": "Parking", "text": "# Parking\nFree parking behind the clinic."}
    )
    assert r.status_code == 201 and r.json()["source_type"] == "text"
    doc_id = r.json()["id"]
    await run_all()
    r = await owner.put(
        f"{K}/documents/{doc_id}/text",
        json={"title": "Parking and access", "text": "Paid parking, ₹50 an hour."},
    )
    assert r.status_code == 200
    await run_all()
    doc = await document(owner, doc_id)
    assert doc["title"] == "Parking and access" and doc["active_version"] == 2
    text = (await owner.get(f"{K}/documents/{doc_id}/text")).json()
    assert text == {"title": "Parking and access", "text": "Paid parking, ₹50 an hour."}
    chunks = (await owner.get(f"{K}/documents/{doc_id}/chunks")).json()
    assert [c["content"] for c in chunks] == ["Paid parking, ₹50 an hour."]


async def test_deleting_a_document_removes_its_files_and_chunks(owner: Actor) -> None:
    from app.modules.storage import s3

    r = await upload(owner, "prices.txt", PRICES.encode())
    doc_id = r.json()["id"]
    async with app_transaction(uuid.UUID(owner.tenant_id)) as s:
        from app.modules.knowledge.models import KnowledgeDocumentVersion

        key = (await s.execute(select(KnowledgeDocumentVersion.object_key))).scalar_one()
    assert key and key.startswith(f"tenants/{owner.tenant_id}/")
    assert await s3.get(key, 10_000) == PRICES.encode()

    assert (await owner.delete(f"{K}/documents/{doc_id}")).status_code == 204
    await drain_after_commit()
    assert await chunk_count(owner.tenant_id) == 0
    assert (await owner.get(f"{K}/documents/{doc_id}")).status_code == 404
    with pytest.raises(s3.StorageUnavailable):
        await s3.get(key, 10_000)


async def test_documents_are_private_to_their_business(owner: Actor, register: RegisterFn) -> None:
    r = await upload(owner, "prices.txt", PRICES.encode())
    doc_id = r.json()["id"]
    other = await register("owner@other.in", "Other Clinic")
    assert (await other.get(f"{K}/documents")).json() == []
    assert (await other.get(f"{K}/documents/{doc_id}")).status_code == 404
    assert (await other.get(f"{K}/documents/{doc_id}/versions/1/download")).status_code == 404
    assert (await other.delete(f"{K}/documents/{doc_id}")).status_code == 404


# ---- web pages ------------------------------------------------------------------------


PAGE = """<!doctype html><html><head><title>Sunrise Dental | Treatments</title>
<style>body{color:red}</style><script>var secret = "do not index";</script></head>
<body><nav><a href="/">Home</a> <a href="/contact">Contact</a></nav>
<main><h1>Treatments</h1><h2>Braces</h2><p>Metal braces cost ₹35,000 for the full course.</p>
<table><tr><th>Item</th><th>Price</th></tr><tr><td>Retainer</td><td>₹4,000</td></tr></table></main>
<footer>© Sunrise Dental</footer></body></html>"""


async def test_web_pages_are_fetched_safely(owner: Actor, web: FakeWeb) -> None:
    web.dns["sunrisedental.example"] = ["93.184.215.14"]
    web.redirect("sunrisedental.example", "/treatments", "/treatments/")
    web.page("sunrisedental.example", "/treatments/", PAGE)
    r = await owner.post(f"{K}/documents/url", json={"url": "https://sunrisedental.example/treatments"})
    assert r.status_code == 201 and r.json()["title"] == "sunrisedental.example"
    doc_id = r.json()["id"]
    await run_all()
    doc = await document(owner, doc_id)
    assert doc["latest"]["status"] == "ready" and doc["title"] == "Sunrise Dental | Treatments"
    assert doc["source_url"] == "https://sunrisedental.example/treatments" and doc["latest"]["downloadable"]
    # Sent to the checked address, with the real host name.
    assert all(str(req.url.host) == "93.184.215.14" for req in web.requests)
    assert web.requests[0].headers["host"] == "sunrisedental.example"
    assert web.requests[0].extensions["sni_hostname"] == "sunrisedental.example"
    [chunk] = (await owner.get(f"{K}/documents/{doc_id}/chunks")).json()
    assert "Metal braces cost ₹35,000" in chunk["content"] and "Retainer | ₹4,000" in chunk["content"]
    assert chunk["heading"] == "Treatments > Braces"
    assert (
        "secret" not in chunk["content"] and "Contact" not in chunk["content"] and "©" not in chunk["content"]
    )

    # Refreshing makes a new version.
    web.page("sunrisedental.example", "/treatments/", PAGE.replace("₹35,000", "₹38,000"))
    r = await owner.post(f"{K}/documents/{doc_id}/refresh")
    assert r.status_code == 200
    await run_all()
    doc = await document(owner, doc_id)
    assert doc["active_version"] == 2 and doc["versions"][1]["status"] == "replaced"
    assert "₹38,000" in (await owner.get(f"{K}/documents/{doc_id}/chunks")).json()[0]["content"]
    r = await owner.post(f"{K}/documents/{doc_id}/versions", files={"file": ("x.txt", b"x")})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "url,code",
    [
        ("ftp://sunrisedental.example/", "invalid_url"),
        ("https://user:pw@sunrisedental.example/", "invalid_url"),
        ("https://sunrisedental.example:8443/", "invalid_url"),
        ("javascript:alert(1)", "invalid_url"),
    ],
)
async def test_invalid_links_are_refused_at_once(owner: Actor, web: FakeWeb, url: str, code: str) -> None:
    r = await owner.post(f"{K}/documents/url", json={"url": url})
    assert r.status_code == 422 and r.json()["error"]["code"] == code


async def test_links_into_private_networks_are_blocked(owner: Actor, web: FakeWeb) -> None:
    web.dns["intranet.example"] = ["10.0.0.5"]
    web.dns["mixed.example"] = ["93.184.215.14", "192.168.1.1"]
    web.dns["mapped.example"] = ["::ffff:127.0.0.1"]
    web.dns["public.example"] = ["93.184.215.14"]
    web.redirect("public.example", "/", "http://169.254.169.254/latest/meta-data/")
    web.dns["loop.example"] = ["93.184.215.14"]
    for i in range(5):
        web.redirect("loop.example", f"/{i}", f"/{i + 1}")
    web.dns["photo.example"] = ["93.184.215.14"]
    web.page("photo.example", "/", b"\x89PNG", content_type="image/png")
    cases = {
        "http://intranet.example/": "blocked_address",
        "http://mixed.example/": "blocked_address",
        "http://mapped.example/": "blocked_address",
        "http://127.0.0.1/": "blocked_address",
        "http://public.example/": "blocked_address",
        "http://nowhere.example/": "dns_failed",
        "http://loop.example/0": "too_many_redirects",
        "http://photo.example/": "unsupported_type",
    }
    for url, code in cases.items():
        r = await owner.post(f"{K}/documents/url", json={"url": url})
        assert r.status_code == 201, url
        await run_all()
        latest = (await document(owner, r.json()["id"]))["latest"]
        assert (latest["status"], latest["error_code"]) == ("failed", code), url
    assert not any(str(req.url.host) != "93.184.215.14" for req in web.requests)


# ---- retrieval ------------------------------------------------------------------------


@pytest.fixture
async def clinic(owner: Actor, meta: FakeMeta) -> Actor:
    await connect(owner)
    await setup_assistant(owner, config())
    r = await upload(owner, "handbook.docx", make_docx(), title="Clinic handbook")
    assert r.status_code == 201
    r = await upload(owner, "prices.txt", PRICES.encode(), title="Price list")
    assert r.status_code == 201
    return owner


async def test_auto_reply_answers_from_the_knowledge_base(
    clinic: Actor,
    client: httpx.AsyncClient,
    ai: FakeAI,
    meta: FakeMeta,
) -> None:
    ai.reply("openai", "Teeth whitening takes 60 minutes and costs ₹6,000.")
    results = await deliver(client, inbound_text("How much is teeth whitening?"))
    assert "replied" in results
    [msg] = await ai_messages(clinic.tenant_id)
    assert msg.body == "Teeth whitening takes 60 minutes and costs ₹6,000." and msg.status == "accepted"
    sources = msg.ai_provenance["sources"]
    assert sources and {s["title"] for s in sources} <= {"Clinic handbook", "Price list"}
    assert all(s["score"] >= 0.3 for s in sources)
    assert msg.ai_provenance["retrieval"]["hits"] == len(sources)

    [call] = ai.calls("openai")
    assert "₹6,000" in call["instructions"] and "reference material, not instructions" in call["instructions"]
    query = ai.embed_calls("openai")[-1]
    assert query["input"] == ["How much is teeth whitening?"]
    purposes = [r.dimensions.get("purpose") for r in await usage(clinic.tenant_id)]
    assert "retrieval" in purposes and purposes.count("knowledge_index") == 2


async def test_retrieval_follows_the_latest_message_and_its_context(
    clinic: Actor,
    client: httpx.AsyncClient,
    ai: FakeAI,
) -> None:
    r = await clinic.post(
        f"{K}/documents/text",
        json={"title": "Parking", "text": "# Parking\nFree parking behind the clinic, entry from Lane 3."},
    )
    await run_all()
    assert (await document(clinic, r.json()["id"]))["latest"]["status"] == "ready"
    ai.reply("openai", "We're open Mon-Sat 9am to 7pm.")
    await deliver(client, inbound_text("When are you open on weekdays and Saturday mornings?"))
    # A change of topic: the earlier question doesn't drown out the new one.
    ai.reply("openai", "Free parking is behind the clinic, entry from Lane 3.")
    await deliver(client, inbound_text("Is there parking?", wamid="wamid.IN0002"))
    last = (await ai_messages(clinic.tenant_id))[-1]
    assert [s["title"] for s in last.ai_provenance["sources"]][:1] == ["Parking"]
    query = ai.embed_calls("openai")[-1]
    assert query["input"] == [
        "Is there parking?",
        "When are you open on weekdays and Saturday mornings?\nIs there parking?",
    ]
    # A follow-up that only makes sense with the earlier question.
    ai.reply("openai", "Teeth whitening costs ₹6,000.")
    await deliver(client, inbound_text("How much is teeth whitening?", wamid="wamid.IN0003"))
    ai.reply("openai", "It takes 60 minutes.")
    await deliver(client, inbound_text("and how long?", wamid="wamid.IN0004"))
    last = (await ai_messages(clinic.tenant_id))[-1]
    assert any("whitening" in c["instructions"] for c in ai.calls("openai")[-1:])
    assert last.ai_provenance["sources"]


async def test_prices_not_in_the_retrieved_passages_are_not_sent(
    clinic: Actor,
    client: httpx.AsyncClient,
    ai: FakeAI,
    meta: FakeMeta,
) -> None:
    # Nothing is close enough with a strict threshold, so ₹6,000 is not grounded.
    await clinic.put(f"{K}/settings", json={"min_score": "0.990"})
    ai.reply("openai", "Whitening is ₹6,000.")
    await deliver(client, inbound_text("Do you do whitening?"))
    [msg] = await ai_messages(clinic.tenant_id)
    assert msg.body == "Thanks! Someone from Sunrise Dental will reply shortly."
    assert msg.ai_provenance["sources"] == [] and msg.ai_provenance["retrieval"]["hits"] == 0
    assert "₹6,000" not in ai.calls("openai")[0]["instructions"]
    assert all("₹6,000" not in s.get("text", {}).get("body", "") for s in meta.sent)


async def test_reply_still_works_when_query_embedding_fails(
    clinic: Actor,
    client: httpx.AsyncClient,
    ai: FakeAI,
) -> None:
    for _ in range(5):
        ai.fail_embedding("openai", 500)
    ai.reply("openai", "We're open Mon-Sat 9am to 7pm.")
    await deliver(client, inbound_text("When are you open?"))
    [msg] = await ai_messages(clinic.tenant_id)
    assert msg.body == "We're open Mon-Sat 9am to 7pm."
    assert msg.ai_provenance["retrieval"]["hits"] == 0 and msg.ai_provenance["retrieval"]["error"]
    ai.embed_failures.clear()


async def test_preview_and_search_show_the_passages(clinic: Actor, ai: FakeAI) -> None:
    ai.reply("openai", "A single implant starts at ₹28,000 including the crown.")
    r = await clinic.post(
        f"{API}/ai/assistant/preview",
        json={"messages": [{"role": "user", "text": "What does a dental implant cost?"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "reply" and body["text"].startswith("A single implant")
    assert body["sources"][0]["title"] == "Price list" and body["retrieval"]["hits"] >= 1

    r = await clinic.post(f"{K}/search", json={"query": "dental implant price"})
    assert r.status_code == 200
    result = r.json()
    assert result["status"] == "ok" and result["min_score"] == 0.3
    top = result["passages"][0]
    assert top["title"] == "Price list" and "₹28,000" in top["content"] and top["used"] is True
    assert [p["score"] for p in result["passages"]] == sorted(
        (p["score"] for p in result["passages"]), reverse=True
    )
    # Below-threshold passages are shown, marked as not used.
    assert len(result["passages"]) == 5 or all(p["used"] for p in result["passages"])

    r = await clinic.post(f"{K}/search", json={"query": "zebra quantum"})
    assert all(p["used"] is False for p in r.json()["passages"])
    assert [r.dimensions.get("purpose") for r in await usage(clinic.tenant_id)].count("search_test") == 2


async def test_search_without_documents_says_so(owner: Actor) -> None:
    r = await owner.post(f"{K}/search", json={"query": "price"})
    assert r.json() == {"passages": [], "min_score": 0.3, "status": "empty"}


# ---- embedding failures and re-indexing ---------------------------------------------


async def test_embedding_failure_can_be_retried(owner: Actor, ai: FakeAI) -> None:
    for _ in range(5):
        ai.fail_embedding("openai", 500)
    r = await upload(owner, "prices.txt", PRICES.encode())
    latest = (await document(owner, r.json()["id"]))["latest"]
    assert latest["status"] == "failed" and "embedding provider failed" in latest["error"]
    failed = [r for r in await usage(owner.tenant_id) if r.dimensions.get("purpose") == "knowledge_index"]
    assert failed and failed[0].dimensions["outcome"] != "ok"
    ai.embed_failures.clear()
    await owner.post(f"{K}/documents/{r.json()['id']}/versions/1/retry")
    await run_all()
    latest = (await document(owner, r.json()["id"]))["latest"]
    assert latest["status"] == "ready" and latest["error"] is None


async def test_wrong_vector_size_is_refused(owner: Actor, ai: FakeAI) -> None:
    ai.embed_dimensions = 768
    r = await upload(owner, "prices.txt", PRICES.encode())
    latest = (await document(owner, r.json()["id"]))["latest"]
    assert latest["status"] == "failed"
    ai.embed_dimensions = None


async def test_changing_the_embedding_model_reindexes_then_switches(
    owner: Actor,
    ai: FakeAI,
    embedding_models: dict[str, uuid.UUID],
) -> None:
    await upload(owner, "prices.txt", PRICES.encode())
    await upload(owner, "handbook.docx", make_docx())
    total = await chunk_count(owner.tenant_id)
    assert await embedding_model_pks(owner.tenant_id) == {embedding_models["openai"]}

    # Gemini has no key yet: the switch waits with an error, answers keep the old model.
    r = await owner.put(
        f"{K}/settings", json={"embedding_provider": "gemini", "embedding_model": "gemini-embedding-test"}
    )
    assert r.status_code == 200
    assert r.json()["pending_embedding_model"]["provider"] == "gemini"
    assert r.json()["embedding_model"]["provider"] == "openai"
    assert await run_all() == ["failed"]
    settings = (await owner.get(f"{K}/settings")).json()
    assert "no usable API key" in settings["reindex_error"]
    assert settings["reindex_done"] == 0 and settings["reindex_total"] == total
    assert (await owner.post(f"{K}/search", json={"query": "whitening"})).json()["status"] == "ok"

    # A provider error during re-indexing is reported and can be retried.
    await enable_provider(owner, "gemini")
    for _ in range(5):
        ai.fail_embedding("gemini", 503)
    r = await owner.post(f"{K}/settings/reindex")
    assert r.status_code == 200 and r.json()["reindex_error"] is None
    assert await run_all() == ["failed"]
    assert "embedding provider failed" in (await owner.get(f"{K}/settings")).json()["reindex_error"]
    ai.embed_failures.clear()

    # A document added during the switch is embedded with both models.
    await owner.post(f"{K}/settings/reindex")
    r = await owner.post(
        f"{K}/documents/text", json={"title": "Parking", "text": "Free parking behind the clinic."}
    )
    assert sorted(await run_all()) == ["ready", "switched"]
    settings = (await owner.get(f"{K}/settings")).json()
    assert settings["embedding_model"]["provider"] == "gemini" and settings["pending_embedding_model"] is None
    assert settings["reindex_error"] is None
    assert await embedding_model_pks(owner.tenant_id) == {embedding_models["gemini"]}
    async with app_transaction(uuid.UUID(owner.tenant_id)) as s:
        vectors = await s.scalar(select(func.count()).select_from(KnowledgeEmbedding))
    assert vectors == await chunk_count(owner.tenant_id)

    gemini = ai.embed_calls("gemini")
    assert gemini[-1]["requests"][0]["embedContentConfig"]["taskType"] == "RETRIEVAL_DOCUMENT"
    r = await owner.post(f"{K}/search", json={"query": "teeth whitening"})
    assert r.json()["status"] == "ok" and r.json()["passages"][0]["used"] is True
    assert ai.embed_calls("gemini")[-1]["requests"][0]["embedContentConfig"]["taskType"] == "RETRIEVAL_QUERY"

    r = await owner.post(f"{K}/settings/reindex")
    assert r.status_code == 422 and r.json()["error"]["code"] == "nothing_to_reindex"


async def test_choosing_a_model_with_no_documents_switches_at_once(owner: Actor) -> None:
    r = await owner.put(
        f"{K}/settings", json={"embedding_provider": "gemini", "embedding_model": "gemini-embedding-test"}
    )
    assert r.json()["embedding_model"]["provider"] == "gemini" and r.json()["pending_embedding_model"] is None
    await drain_after_commit()
    assert not any(job.task == KNOWLEDGE_REINDEX for job in DISPATCHED)


# ---- sweeper --------------------------------------------------------------------------


async def test_sweeper_redispatches_lost_and_stuck_work(owner: Actor, su_conn: Any) -> None:
    from app.modules.knowledge.sweeper import sweep

    r = await upload(owner, "prices.txt", PRICES.encode(), run=False)
    await drain_after_commit()
    DISPATCHED.clear()  # the dispatch was lost
    assert await sweep() == {"versions": 0, "reindexes": 0}  # too recent to tell
    await su_conn.execute("UPDATE knowledge_document_versions SET updated_at = now() - interval '5 minutes'")
    assert await sweep() == {"versions": 1, "reindexes": 0}
    assert await run_all() == ["ready"]
    assert (await document(owner, r.json()["id"]))["latest"]["status"] == "ready"

    # A worker died mid-way: the version stays `processing` until it is stale.
    r = await upload(owner, "other.txt", b"Open on Saturdays.", run=False)
    await drain_after_commit()
    DISPATCHED.clear()
    await su_conn.execute(
        "UPDATE knowledge_document_versions SET status = 'processing', "
        "updated_at = now() - interval '5 minutes' WHERE status = 'queued'"
    )
    assert await sweep() == {"versions": 0, "reindexes": 0}
    r2 = await owner.post(f"{K}/documents/{r.json()['id']}/versions/1/retry")
    assert r2.status_code == 409
    await su_conn.execute(
        "UPDATE knowledge_document_versions SET updated_at = now() - interval '20 minutes' "
        "WHERE status = 'processing'"
    )
    assert await sweep() == {"versions": 1, "reindexes": 0}
    assert await run_all() == ["ready"]

    # A re-index that stopped is picked up too.
    await enable_provider(owner, "gemini")
    await owner.put(
        f"{K}/settings", json={"embedding_provider": "gemini", "embedding_model": "gemini-embedding-test"}
    )
    await drain_after_commit()
    DISPATCHED.clear()
    assert (await sweep())["reindexes"] == 0  # may still be running
    await su_conn.execute("UPDATE knowledge_settings SET updated_at = now() - interval '6 minutes'")
    assert (await sweep())["reindexes"] == 1
    assert await run_all() == ["switched"]


async def test_running_the_same_version_twice_is_harmless(owner: Actor) -> None:
    from app.modules.knowledge.service import process_version

    r = await upload(owner, "prices.txt", PRICES.encode(), run=False)
    await drain_after_commit()
    job = next(j for j in DISPATCHED if j.task == KNOWLEDGE_PROCESS)
    ids = [uuid.UUID(x) for x in job.args]
    assert await process_version(*ids) == "ready"
    assert await process_version(*ids) == "ready"  # already done: reported, not redone
    chunks = await chunk_count(owner.tenant_id)
    DISPATCHED.clear()
    assert await chunk_count(owner.tenant_id) == chunks
    assert (await document(owner, r.json()["id"]))["latest"]["status"] == "ready"


@pytest.fixture(autouse=True)
async def _reset_web_override() -> AsyncIterator[None]:
    yield
    from app.modules.knowledge import webfetch

    webfetch.set_transport_override(None)
