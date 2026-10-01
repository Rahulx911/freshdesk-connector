"""Behaviours a merchant's security review would ask about."""

from __future__ import annotations

import json
import logging

import pytest

from woocommerce_connector.observability import JsonFormatter, audit, mask_value


def test_logs_are_json_with_a_timestamp():
    rec = logging.LogRecord("woocommerce_connector", logging.INFO, __file__, 1, "hello", None, None)
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["ts"].endswith("Z")


@pytest.mark.parametrize("raw,must_not_contain", [
    ("customer ananya.rao@example.com asked", "ananya.rao"),
    ("phone 9845011223", "9845011223"),
    ("key ck_abcdefghijklmnopqrstuvwxyz0123456789", "wxyz0123456789"),
])
def test_audit_arguments_are_masked(raw, must_not_contain):
    assert must_not_contain not in mask_value(raw)


def test_mask_value_recurses_into_structures():
    out = mask_value({"a": ["x@y.com"], "b": {"c": "9845011223"}})
    blob = json.dumps(out)
    assert "x@y.com" not in blob
    assert "9845011223" not in blob


def test_audit_emits_one_line_per_call(caplog):
    with caplog.at_level(logging.INFO, logger="woocommerce_connector"):
        with audit("get_order", {"order_id": 1}):
            pass
    events = [r for r in caplog.records if getattr(r, "extra_fields", {}).get("event") == "tool_call"]
    assert len(events) == 1
    assert events[0].extra_fields["outcome"] == "ok"


def test_audit_records_failures_and_reraises(caplog):
    class Boom(Exception):
        code = "kaboom"

    with caplog.at_level(logging.INFO, logger="woocommerce_connector"):
        with pytest.raises(Boom):
            with audit("get_order", {"order_id": 1}):
                raise Boom("nope")
    events = [r for r in caplog.records if getattr(r, "extra_fields", {}).get("event") == "tool_call"]
    assert events[0].extra_fields["outcome"] == "error"
    assert events[0].extra_fields["error_code"] == "kaboom"


async def test_no_credential_ever_appears_in_a_tool_response(service):
    """The consumer secret must not be reachable through any tool output."""
    blobs = [
        json.dumps(await service.connector_status(), default=str),
        json.dumps(await service.get_order(1101), default=str),
        json.dumps(await service.store_pulse(days=30), default=str),
    ]
    from mock_server import data as mock_data
    for blob in blobs:
        assert mock_data.CONSUMER_SECRET not in blob
        assert mock_data.CONSUMER_KEY not in blob


async def test_status_reports_read_only_mode(service):
    st = await service.connector_status()
    assert "read-only" in st["mode"].lower()
    assert "cannot write" in st["mode"].lower()
