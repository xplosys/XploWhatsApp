"""Stand-ins for Meta's Graph API and the AI providers, for end-to-end and load tests.

The API and workers are pointed here with META_GRAPH_BASE_URL and *_BASE_URL (see
e2e/docker-compose.e2e.yml), so the whole product runs for real: only the far side of
those HTTP calls is simulated. The Graph and AI behaviour is the same code the backend's
integration tests use (backend/tests/support), served over HTTP.

Routes:
  /graph/...              Graph API (Embedded Signup token exchange, WABA, numbers, sends)
  /ai/<provider>/...      OpenAI, Gemini and DeepSeek APIs (models, chat, embeddings)
  /_control/...           for the tests: reset, queue AI answers, read what was sent, and
                          deliver a customer message to the API as a signed Meta webhook

Run: PYTHONPATH=backend uvicorn e2e.fakes.server:app --port 9000
Never point a production deployment at this.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import statistics
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

import tests.support.meta as meta_support
from tests.support.ai import HOSTS, FakeAI

APP_ID = os.environ.get("META_APP_ID", "1234567890")
APP_SECRET = os.environ.get("META_APP_SECRET", "e2e-app-secret")
WEBHOOK_URL = os.environ.get("E2E_WEBHOOK_URL", "http://api:8000/api/v1/webhooks/whatsapp")
# Answer chat calls with this when no answer is queued (load tests); unset: fail loudly.
DEFAULT_ANSWER = os.environ.get("E2E_DEFAULT_AI_ANSWER")
# How long a chat answer takes, like a real model (load tests); embeddings stay instant.
AI_LATENCY_SECONDS = float(os.environ.get("E2E_AI_LATENCY_MS", "0")) / 1000

PROVIDER_HOSTS = {provider: host for host, provider in HOSTS.items()}

app = FastAPI(title="XploWhatsApp test fakes", docs_url=None, redoc_url=None)
state: dict[str, Any] = {}
client = httpx.AsyncClient(timeout=10)


def _new_ids() -> tuple[str, str]:
    """Fresh WhatsApp account and number IDs, so each run onboards a number no earlier
    run has claimed (a number can belong to one business only)."""
    return str(10**14 + secrets.randbelow(9 * 10**14)), str(10**14 + secrets.randbelow(9 * 10**14))


def reset() -> None:
    waba_id, phone_id = _new_ids()
    # FakeMeta reads these module constants when it handles a request.
    meta_support.WABA_ID = waba_id
    meta_support.PHONE_NUMBER_ID = phone_id
    meta = meta_support.FakeMeta(app_id=APP_ID)
    meta.templates = [
        {
            "id": "1013",
            "name": "appointment_reminder",
            "language": "en",
            "category": "UTILITY",
            "status": "APPROVED",
            "components": [{"type": "BODY", "text": "Hi {{1}}, your appointment is on {{2}}."}],
        }
    ]
    state.update(
        meta=meta,
        ai=FakeAI(),
        waba_id=waba_id,
        phone_id=phone_id,
        inbound=0,
        # For load tests: when each customer last wrote, and how long their reply took.
        waiting={},
        reply_seconds=[],
        ack_ms=[],
    )


reset()


async def _forward(handler: Any, request: Request, url: httpx.URL) -> Response:
    body = await request.body()
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in (b"host", b"content-length")]
    try:
        result = handler(httpx.Request(request.method, url, headers=headers, content=body))
    except AssertionError as exc:  # the fake found a malformed request: make it visible
        return Response(json.dumps({"error": {"message": f"fake rejected request: {exc}"}}), 400)
    return Response(result.content, status_code=result.status_code, media_type="application/json")


@app.api_route("/graph/{path:path}", methods=["GET", "POST", "DELETE"])
async def graph(path: str, request: Request) -> Response:
    url = httpx.URL(f"https://graph.facebook.com/{path}", params=request.url.query)
    response = await _forward(state["meta"].handler, request, url)
    sent = request.method == "POST" and re.fullmatch(r"v[\d.]+/\d+/messages", path)
    if sent and response.status_code == 200:
        to = json.loads(await request.body()).get("to")
        started = state["waiting"].pop(to, None)
        if started is not None:
            state["reply_seconds"].append(time.time() - started)
    return response


@app.api_route("/ai/{provider}/{path:path}", methods=["GET", "POST"])
async def ai(provider: str, path: str, request: Request) -> Response:
    if provider not in PROVIDER_HOSTS:
        raise HTTPException(404)
    fake: FakeAI = state["ai"]
    is_embedding = path.endswith("embeddings") or path.endswith(":batchEmbedContents")
    if request.method == "POST" and not is_embedding:
        if not fake.queues[provider] and DEFAULT_ANSWER:
            fake.reply(provider, DEFAULT_ANSWER)
        await asyncio.sleep(AI_LATENCY_SECONDS)
    url = httpx.URL(f"https://{PROVIDER_HOSTS[provider]}/{path}", params=request.url.query)
    return await _forward(fake.handler, request, url)


# ---- control ----------------------------------------------------------------------


class Answer(BaseModel):
    text: str
    needs_human: bool = False


class Inbound(BaseModel):
    text: str
    wa_id: str = "919812345678"
    name: str = "Priya"


@app.post("/_control/reset")
async def control_reset() -> dict[str, str]:
    reset()
    return {"waba_id": state["waba_id"], "phone_number_id": state["phone_id"]}


@app.get("/_control/meta")
async def control_meta() -> dict[str, Any]:
    meta = state["meta"]
    return {
        "waba_id": state["waba_id"],
        "phone_number_id": state["phone_id"],
        "subscribed": meta.subscribed,
        "registered": meta.registered,
        "sent": meta.sent,
    }


@app.post("/_control/ai/{provider}/answers", status_code=201)
async def control_answer(provider: str, answer: Answer) -> dict[str, int]:
    if provider not in PROVIDER_HOSTS:
        raise HTTPException(404)
    state["ai"].reply(provider, answer.text, needs_human=answer.needs_human)
    return {"queued": len(state["ai"].queues[provider])}


@app.get("/_control/ai/{provider}/calls")
async def control_calls(provider: str) -> list[dict[str, Any]]:
    """The chat requests the product sent to this provider, newest last."""
    if provider not in PROVIDER_HOSTS:
        raise HTTPException(404)
    calls: list[dict[str, Any]] = state["ai"].calls(provider)
    return calls


@app.post("/_control/inbound")
async def control_inbound(message: Inbound) -> dict[str, Any]:
    """Deliver a customer's WhatsApp message to the API exactly as Meta would: the
    documented webhook payload, signed with the app secret."""
    state["inbound"] += 1
    wamid = f"wamid.E2E{int(time.time() * 1000)}{state['inbound']}"
    payload = meta_support.inbound_text(
        message.text,
        wamid=wamid,
        wa_id=message.wa_id,
        bsuid=None,
        name=message.name,
        phone_number_id=state["phone_id"],
    )
    payload["entry"][0]["id"] = state["waba_id"]
    body, headers = meta_support.signed(payload, APP_SECRET)
    state["waiting"].setdefault(message.wa_id, time.time())
    started = time.perf_counter()
    r = await client.post(WEBHOOK_URL, content=body, headers=headers)
    ack_ms = (time.perf_counter() - started) * 1000
    state["ack_ms"].append(ack_ms)
    return {"status": r.status_code, "wamid": wamid, "ack_ms": round(ack_ms, 1)}


def _percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(pick(0.95), 3),
        "p99": round(pick(0.99), 3),
        "max": round(ordered[-1], 3),
    }


@app.post("/_control/stats-reset")
async def control_stats_reset() -> dict[str, bool]:
    """Clears the load-test counters but keeps the connected number and queued answers."""
    state.update(inbound=0, waiting={}, reply_seconds=[], ack_ms=[])
    return {"ok": True}


@app.get("/_control/stats")
async def control_stats() -> dict[str, Any]:
    """For load tests: customer messages delivered, replies sent through the WhatsApp
    API, customers still waiting, webhook acknowledgement times (ms) and time from a
    customer's message to the reply leaving (seconds)."""
    return {
        "inbound": state["inbound"],
        "replies": len(state["reply_seconds"]),
        "waiting": len(state["waiting"]),
        "webhook_ack_ms": _percentiles(state["ack_ms"]),
        "reply_seconds": _percentiles(state["reply_seconds"]),
    }


@app.get("/_control/health")
async def control_health() -> dict[str, str]:
    return {"status": "ok"}
