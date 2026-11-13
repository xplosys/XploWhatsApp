"""Metrics and error reporting: what Prometheus can read, and what never leaves."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core import metrics
from app.core.config import Environment, get_settings
from app.core.observability import _scrub
from app.workers.celery_app import _task_finished, _task_started

API = "/api/v1"


def sample(text: str, name: str, **labels: str) -> float | None:
    want = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    for line in text.splitlines():
        if line.startswith(name + "{"):
            got = line[len(name) + 1 : line.rindex("}")]
            if ",".join(sorted(got.split(","))) == want:
                return float(line.rsplit(" ", 1)[1])
    return None


async def test_metrics_endpoint_access(
    client: httpx.AsyncClient, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    # Development without a token: open, for local Prometheus.
    assert (await client.get("/metrics")).status_code == 200
    # Production without a token: off.
    monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
    assert (await client.get("/metrics")).status_code == 404
    # With a token: only the right bearer token.
    monkeypatch.setattr(settings, "metrics_token", SecretStr("scrape-token-123"))
    assert (await client.get("/metrics")).status_code == 404
    assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 404
    r = await client.get("/metrics", headers={"Authorization": "Bearer scrape-token-123"})
    assert r.status_code == 200 and "xplo_queue_depth" in r.text


async def test_requests_are_counted_by_route_template(client: httpx.AsyncClient, database: Any) -> None:
    lead = uuid.uuid4()
    before = (await client.get("/metrics")).text
    base = sample(
        before, "xplo_http_requests_total", method="GET", route=f"{API}/leads/{{lead_id}}", status="401"
    )
    await client.get(f"{API}/leads/{lead}")
    await client.get("/no-such-page")
    text = (await client.get("/metrics")).text
    after = sample(
        text, "xplo_http_requests_total", method="GET", route=f"{API}/leads/{{lead_id}}", status="401"
    )
    assert after == (base or 0) + 1
    assert str(lead) not in text  # ids never become labels
    assert sample(text, "xplo_http_requests_total", method="GET", route="unmatched", status="404")
    assert "/no-such-page" not in text


def test_task_results_become_bounded_labels() -> None:
    assert metrics.result_label("replied") == "replied"
    assert metrics.result_label("handover") == "handover"
    assert metrics.result_label(3) == "ok"
    assert metrics.result_label({"sent": 2}) == "ok"
    assert metrics.result_label("019a-3f2c-uuid-like") == "ok"
    assert metrics.result_label("A sentence with Spaces") == "ok"

    class Task:
        name = "app.workers.tasks.messages.ai_auto_reply"

    def count(result: str) -> float:
        return metrics.TASKS.labels("messages.ai_auto_reply", result)._value.get()  # type: ignore[attr-defined]

    replied, errors = count("replied"), count("error")
    _task_started(task_id="t1")
    _task_finished(task_id="t1", task=Task(), retval="replied", state="SUCCESS")
    _task_finished(task_id="t2", task=Task(), retval=None, state="FAILURE")
    assert count("replied") == replied + 1 and count("error") == errors + 1


def test_sentry_events_carry_no_bodies_cookies_or_secrets() -> None:
    event = {
        "request": {
            "data": '{"text": "my card number is 4111"}',
            "cookies": {"xplo_refresh": "abc"},
            "headers": {"Authorization": "Bearer eyJ...", "X-Hub-Signature-256": "sha256=1", "Accept": "*/*"},
        },
        "extra": {"api_key": "sk-live-123", "conversation": "fine"},
    }
    out = _scrub(event, {})
    assert out is not None
    assert "data" not in out["request"] and "cookies" not in out["request"]
    assert out["request"]["headers"]["Authorization"] == "[redacted]"
    assert out["request"]["headers"]["X-Hub-Signature-256"] == "[redacted]"
    assert out["request"]["headers"]["Accept"] == "*/*"
    assert "sk-live-123" not in str(out["extra"])
