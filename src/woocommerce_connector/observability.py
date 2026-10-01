"""Structured logs, an audit line per tool call, and Prometheus metrics.

The audit line is the artefact a merchant's security review asks for: who
called what, with which arguments, how many upstream requests it cost, and
whether it failed. Arguments are masked before they are written, so a search
for a customer email never lands in the log in clear text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any

from prometheus_client import Counter, Histogram

TOOL_CALLS = Counter("woo_tool_calls_total", "MCP tool calls", ["tool", "outcome"])
TOOL_LATENCY = Histogram("woo_tool_latency_seconds", "MCP tool latency", ["tool"])
UPSTREAM_REQUESTS = Counter("woo_upstream_requests_total", "WooCommerce REST requests", ["tool"])

_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")
_LONG_DIGITS = re.compile(r"\b\d{7,}\b")
_KEYS = re.compile(r"\b(ck|cs)_[A-Za-z0-9]{8,}\b")

log = logging.getLogger("woocommerce_connector")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)   # stdout is the MCP channel
    if os.environ.get("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger("woocommerce_connector")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def mask_value(value: Any) -> Any:
    """Mask anything that looks like personal data or a credential."""
    if isinstance(value, str):
        out = _EMAIL.sub(lambda m: f"***@{m.group(1)}", value)
        out = _LONG_DIGITS.sub(lambda m: "*" * (len(m.group(0)) - 4) + m.group(0)[-4:], out)
        out = _KEYS.sub(lambda m: m.group(0)[:6] + "…", out)
        return out
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    return value


@contextmanager
def audit(tool: str, args: dict, *, tenant: str | None = None):
    """Emit exactly one audit line per tool call, success or failure."""
    started = time.perf_counter()
    outcome = "ok"
    error_code = None
    try:
        yield
    except Exception as exc:  # re-raised; the MCP layer turns it into JSON
        outcome = "error"
        error_code = getattr(exc, "code", type(exc).__name__)
        raise
    finally:
        elapsed = time.perf_counter() - started
        TOOL_CALLS.labels(tool=tool, outcome=outcome).inc()
        TOOL_LATENCY.labels(tool=tool).observe(elapsed)
        if os.environ.get("AUDIT_LOG", "on").lower() != "off":
            record = logging.LogRecord(
                "woocommerce_connector", logging.INFO, __file__, 0, "tool_call", None, None
            )
            record.extra_fields = {
                "event": "tool_call",
                "tool": tool,
                "outcome": outcome,
                "error_code": error_code,
                "duration_ms": round(elapsed * 1000, 1),
                "tenant": tenant,
                "args": mask_value(args),
            }
            log.handle(record)
