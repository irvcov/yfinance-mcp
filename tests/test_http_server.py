"""Tests for the Streamable HTTP transport (no network access needed)."""

import json
import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from wrapper.http_server import app  # noqa: E402

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _parse(resp):
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
    return resp.json()


def _init(client):
    resp = client.post(
        "/mcp",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    return resp


def test_initialize_and_list_tools():
    with TestClient(app) as client:
        resp = _init(client)
        assert resp.status_code == 200
        assert _parse(resp)["result"]["serverInfo"]["name"] == "mcp-yfinance"
        sid = resp.headers["mcp-session-id"]
        h = {**HEADERS, "mcp-session-id": sid}
        client.post(
            "/mcp",
            headers=h,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        resp = client.post(
            "/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert resp.status_code == 200
        assert len(_parse(resp)["result"]["tools"]) > 0


def test_non_mcp_request_rejected():
    with TestClient(app) as client:
        resp = client.post("/mcp", headers=HEADERS, json={"foo": "bar"})
        assert resp.status_code >= 400


def test_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_import_from_clean_process():
    code = "import wrapper.http_server as m; print(m.app.title)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": str(SRC), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
