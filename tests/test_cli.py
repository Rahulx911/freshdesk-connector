from __future__ import annotations

import json
import subprocess
import sys

from woocommerce_connector.cli import build_parser, main


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "woocommerce_connector.cli", *args],
        capture_output=True, text=True, timeout=120,
    )


def test_export_spec_lists_eleven_tools(tmp_path):
    out = tmp_path / "spec.json"
    assert main(["export-spec", "-o", str(out)]) == 0
    spec = json.loads(out.read_text())
    assert len(spec["tools"]) == 11
    assert len(spec["prompts"]) == 2
    assert all(t["annotations"]["readOnlyHint"] for t in spec["tools"])
    assert spec["server"]["mode"] == "read-only"


def test_export_spec_matches_the_committed_file(tmp_path):
    """docs/mcp_tool_spec.json is a deliverable; keep it regenerated."""
    import pathlib
    out = tmp_path / "spec.json"
    main(["export-spec", "-o", str(out)])
    committed = pathlib.Path("docs/mcp_tool_spec.json")
    assert committed.exists(), "run: woocommerce-connector export-spec -o docs/mcp_tool_spec.json"
    assert json.loads(out.read_text()) == json.loads(committed.read_text()), (
        "docs/mcp_tool_spec.json is stale; regenerate it"
    )


def test_status_without_credentials_is_a_clean_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("WOO_CONNECTOR_HOME", str(tmp_path / "empty"))
    for var in ("WOO_STORE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert main(["auth", "status"]) == 1


def test_parser_rejects_unknown_command():
    import pytest
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonsense"])


def test_help_runs():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "woocommerce-connector" in result.stdout
