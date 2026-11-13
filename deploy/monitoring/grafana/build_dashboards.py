"""Writes the Grafana dashboards in dashboards/. Edit here and rerun, so the JSON stays
consistent: python deploy/monitoring/grafana/build_dashboards.py"""

from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


def panel(title, exprs, x, y, w=12, h=8, unit="short", kind="timeseries", desc="", stack=False):
    targets = [{"datasource": DS, "expr": e, "legendFormat": l, "refId": chr(65 + i)} for i, (e, l) in enumerate(exprs)]
    p = {
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {},
    }
    if kind == "timeseries":
        p["fieldConfig"]["defaults"]["custom"] = {"stacking": {"mode": "normal" if stack else "none"}, "fillOpacity": 10}
        p["options"] = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
                        "tooltip": {"mode": "multi"}}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
    return p


def dashboard(uid, title, panels, desc):
    for i, p in enumerate(panels):
        p["id"] = i + 1
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["xplowhatsapp"],
        "timezone": "browser",
        "schemaVersion": 41,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
        "templating": {"list": []},
        "annotations": {"list": []},
        "editable": False,
    }


app = dashboard(
    "xplo-app",
    "XploWhatsApp: app",
    [
        panel("Requests per second", [("sum(rate(xplo_http_requests_total[5m]))", "all")], 0, 0, 6, 4, "reqps", "stat"),
        panel("Server errors", [('(sum(rate(xplo_http_requests_total{status=~"5.."}[5m])) or vector(0)) / clamp_min(sum(rate(xplo_http_requests_total[5m])), 0.001)', "5xx")],
              6, 0, 6, 4, "percentunit", "stat"),
        panel("p95 latency", [("histogram_quantile(0.95, sum by (le) (rate(xplo_http_request_duration_seconds_bucket[5m])))", "p95")],
              12, 0, 6, 4, "s", "stat"),
        panel("Waiting tasks", [("sum(xplo_queue_depth)", "queued")], 18, 0, 6, 4, "short", "stat"),
        panel("Requests by status", [("sum by (status) (rate(xplo_http_requests_total[5m]))", "{{status}}")], 0, 4, 12, 8, "reqps",
              stack=True),
        panel("Slowest routes (p95)", [('topk(8, histogram_quantile(0.95, sum by (le, route) (rate(xplo_http_request_duration_seconds_bucket[5m]))))', "{{route}}")],
              12, 4, 12, 8, "s"),
        panel("WhatsApp sends", [('sum by (result) (increase(xplo_task_runs_total{task="messages.send_outbox_job"}[5m]))', "{{result}}")],
              0, 12, 12, 8, "short", desc="done = accepted by Meta; failed = refused or out of retries; retry = will try again.", stack=True),
        panel("AI auto-replies", [('sum by (result) (increase(xplo_task_runs_total{task="messages.ai_auto_reply"}[5m]))', "{{result}}")],
              12, 12, 12, 8, "short", desc="replied, handover to a person, or why no reply was needed (ai_off, opted_out, window_closed...).",
              stack=True),
        panel("AI provider calls", [("sum by (provider, result) (increase(xplo_ai_attempts_total[5m]))", "{{provider}} {{result}}")],
              0, 20, 12, 8, "short", stack=True),
        panel("AI provider latency (p95)", [("histogram_quantile(0.95, sum by (le, provider) (rate(xplo_ai_attempt_duration_seconds_bucket[5m])))", "{{provider}}")],
              12, 20, 12, 8, "s"),
        panel("Queues", [("xplo_queue_depth", "{{queue}}")], 0, 28, 12, 8, "short"),
        panel("Task errors", [('sum by (task) (increase(xplo_task_runs_total{result="error"}[5m]))', "{{task}}")], 12, 28, 12, 8, "short"),
        panel("Webhooks received", [('sum by (route, status) (rate(xplo_http_requests_total{route=~".*/webhooks/.*"}[5m]))', "{{route}} {{status}}")],
              0, 36, 12, 8, "reqps", desc="401 means a bad signature."),
        panel("Knowledge processing", [('sum by (result) (increase(xplo_task_runs_total{task="knowledge.process_version"}[15m]))', "{{result}}")],
              12, 36, 12, 8, "short", stack=True),
    ],
    "Traffic, errors, latency, WhatsApp sends, AI replies and queues.",
)

infra = dashboard(
    "xplo-infra",
    "XploWhatsApp: server",
    [
        panel("CPU used", [('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', "cpu")], 0, 0, 6, 4, "percentunit", "stat"),
        panel("Memory available", [("node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "available")], 6, 0, 6, 4,
              "percentunit", "stat"),
        panel("Disk free", [('node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}', "free")],
              12, 0, 6, 4, "percentunit", "stat"),
        panel("Last backup", [("time() - xplo_backup_last_success_timestamp_seconds", "age")], 18, 0, 6, 4, "s", "stat",
              desc="Alerts after 26 hours."),
        panel("CPU", [('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', "used")], 0, 4, 12, 8, "percentunit"),
        panel("Memory", [("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used"), ("node_memory_MemTotal_bytes", "total")],
              12, 4, 12, 8, "bytes"),
        panel("Postgres connections", [("sum by (state) (pg_stat_activity_count)", "{{state}}"), ("max(pg_settings_max_connections)", "max")],
              0, 12, 12, 8, "short"),
        panel("Postgres database size", [('pg_database_size_bytes{datname="xplowhatsapp"}', "size")], 12, 12, 12, 8, "bytes"),
        panel("Redis memory", [("redis_memory_used_bytes", "used")], 0, 20, 12, 8, "bytes"),
        panel("Disk I/O", [("sum(rate(node_disk_read_bytes_total[5m]))", "read"), ("sum(rate(node_disk_written_bytes_total[5m]))", "write")],
              12, 20, 12, 8, "Bps"),
    ],
    "CPU, memory, disk, Postgres, Redis and backups.",
)

OUT.mkdir(exist_ok=True)
for d in (app, infra):
    (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2) + "\n")
print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.json"))))
