"""The merchant-facing auth flow, end to end against a real HTTP mock:
login (bad key, good key) -> status -> logout, plus spec export."""

import io
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from freshdesk_connector import cli

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def mock_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_server.app:app", "--port", str(port),
                          "--log-level", "warning"], cwd=ROOT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    p.terminate()
    p.wait(timeout=5)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FRESHDESK_CONNECTOR_HOME", str(tmp_path))
    monkeypatch.delenv("FRESHDESK_DOMAIN", raising=False)
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    return tmp_path


def _login(monkeypatch, url, key):
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    return cli.main(["auth", "login", "--domain", url, "--api-key-stdin"])


@pytest.mark.slow
def test_login_status_logout(mock_url, home, monkeypatch, capsys):
    assert _login(monkeypatch, mock_url, "wrong-key") == 1
    assert not (home / "credentials.json").exists()            # bad key never stored
    assert "Login failed" in capsys.readouterr().err

    assert _login(monkeypatch, mock_url, "mock-api-key-123") == 0
    cred = home / "credentials.json"
    assert stat.S_IMODE(os.stat(cred).st_mode) == 0o600
    assert "Demo Support Agent" in capsys.readouterr().out

    assert cli.main(["auth", "status"]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out and "mock-api-key-123" not in out

    assert cli.main(["auth", "logout"]) == 0
    assert not cred.exists()
    assert cli.main(["auth", "status"]) == 1
    assert "not_configured" in capsys.readouterr().out


def test_bad_domain_rejected(home, monkeypatch, capsys):
    assert _login(monkeypatch, "http://evil.example.com", "k") == 1
    assert "localhost" in capsys.readouterr().err


def test_export_spec_matches_committed_file(tmp_path):
    out = tmp_path / "spec.json"
    assert cli.main(["export-spec", "-o", str(out)]) == 0
    fresh = json.loads(out.read_text())
    committed = json.loads((ROOT / "docs" / "mcp_tool_spec.json").read_text())
    assert fresh == committed, "docs/mcp_tool_spec.json is stale: run `freshdesk-connector export-spec -o docs/mcp_tool_spec.json`"
    assert len(fresh["tools"]) == 11
