"""Structured logs, Prometheus metrics and a per-call audit trail.

Logged/labelled: tool name, tenant id, outcome code, latency, Freshdesk
credits and upstream calls. Never logged: API keys, bearer tokens, and raw
argument values that may hold customer PII (emails and phones are masked)."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from .normalize import mask_pii

REGISTRY = CollectorRegistry()

TOOL_CALLS = Counter("fdconn_tool_calls_total", "MCP tool calls", ["tool", "tenant", "outcome"],
                     registry=REGISTRY)
TOOL_LATENCY = Histogram("fdconn_tool_latency_seconds", "MCP tool latency", ["tool"],
                         buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30), registry=REGISTRY)
CREDITS = Counter("fdconn_freshdesk_credits_total", "Freshdesk API credits spent", ["tenant"],
                  registry=REGISTRY)
UPSTREAM = Counter("fdconn_freshdesk_requests_total", "HTTP requests sent to Freshdesk", ["tenant"],
                   registry=REGISTRY)
GUARDRAIL = Counter("fdconn_guardrail_events_total", "Guardrail interventions", ["kind", "tenant"],
                    registry=REGISTRY)

audit_log = logging.getLogger("freshdesk_connector.audit")


def metrics_payload() -> bytes:
    return generate_latest(REGISTRY)


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if v is None or v == [] or v is False:
            continue
        out[k] = mask_pii(v) if isinstance(v, str) else v
    return out


def record_tool_call(*, tool: str, tenant: str, outcome: str, started: float, credits: int,
                     upstream: int, args: dict[str, Any], response_chars: int | None = None,
                     guardrails: list[str] | None = None) -> None:
    latency = time.perf_counter() - started
    TOOL_CALLS.labels(tool, tenant, outcome).inc()
    TOOL_LATENCY.labels(tool).observe(latency)
    if credits:
        CREDITS.labels(tenant).inc(credits)
    if upstream:
        UPSTREAM.labels(tenant).inc(upstream)
    for g in guardrails or []:
        GUARDRAIL.labels(g, tenant).inc()
    audit_log.info("tool_call", extra={"event": {
        "call_id": uuid.uuid4().hex[:12], "tool": tool, "tenant": tenant, "outcome": outcome,
        "latency_ms": round(latency * 1000, 1), "freshdesk_credits": credits,
        "upstream_requests": upstream, "args": redact_args(args),
        "response_chars": response_chars, "guardrails": guardrails or [],
    }})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(event)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname} {record.name}: {record.getMessage()}"
        event = getattr(record, "event", None)
        return f"{base} {json.dumps(event, default=str)}" if isinstance(event, dict) else base


def configure_logging(level: str = "WARNING", fmt: str = "text") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)   # stdout is the MCP stdio channel
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # The audit trail is on at INFO regardless of app log level; AUDIT_LOG=off silences it
    # (local demos/evals only - keep it on in production).
    audit_on = os.environ.get("AUDIT_LOG", "on").lower() not in ("off", "0", "false")
    logging.getLogger("freshdesk_connector.audit").setLevel(logging.INFO if audit_on else logging.CRITICAL)
    # httpx logs full URLs at INFO, which can include customer emails/phones in query strings
    logging.getLogger("httpx").setLevel(logging.WARNING)
