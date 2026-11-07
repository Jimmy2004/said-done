"""Tests for the cloud transcript transport client and record normalization.

Everything here runs against a fake stdio server built inside pytest's tmp
directory: no network, no real host, no real credentials.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from said_done.ingestion import cloud_mcp_client as cloud_mcp_module
from said_done.ingestion.cloud_mcp_client import (
    DEFAULT_TIMEOUT,
    MAX_TEXT,
    SOURCE,
    CloudTranscriptClient,
    CloudTranscriptError,
    CloudTranscriptTimeout,
    normalize_transcript,
)

FAKE_SERVER = r'''
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
pid_file = sys.argv[2] if len(sys.argv) > 2 else ""
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
if mode == "exit":
    sys.exit(3)


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    ident = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-cloud-server", "version": "1.0.0"}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        if mode == "hang":
            continue
        if mode == "wedge":
            time.sleep(600)
            continue
        params = message.get("params") or {}
        if mode == "error":
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text":
                             "token sk_test_FAKE0123456789 https://example.invalid/v1"}],
                "isError": True}})
        elif mode == "badjson":
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text": "not json"}]}})
        else:
            payload = {"tool": params.get("name"), "results": [], "resultCount": 0,
                       "truncated": False, "source": "fake",
                       "echo": params.get("arguments")}
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}})
    elif ident is not None:
        send({"jsonrpc": "2.0", "id": ident,
              "error": {"code": -32601, "message": "method not found"}})
'''


@pytest.fixture()
def server(tmp_path):
    """Build a fake stdio MCP server and return a factory for its command."""

    def build(mode: str):
        script = tmp_path / f"fake_mcp_{mode}.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        pid_file = tmp_path / f"pid_{mode}.txt"
        return [sys.executable, str(script), mode, str(pid_file)], pid_file

    return build


def _pid(pid_file: Path) -> int:
    return int(pid_file.read_text(encoding="utf-8").strip())


def _running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# -- command wiring -------------------------------------------------


def test_client_requires_an_explicit_command():
    with pytest.raises(TypeError):
        CloudTranscriptClient()
    with pytest.raises(ValueError):
        CloudTranscriptClient(command=[])
    with pytest.raises(ValueError):
        CloudTranscriptClient(command=None)
    with pytest.raises(ValueError):
        CloudTranscriptClient(command="not-an-array")
    with pytest.raises(ValueError):
        CloudTranscriptClient(command=[""])
    with pytest.raises(ValueError):
        CloudTranscriptClient(command=[123])


def test_client_has_no_builtin_default_transport():
    assert not hasattr(cloud_mcp_module, "default_command")
    assert not hasattr(cloud_mcp_module, "resolve_wsl_executable")


def test_client_uses_the_supplied_command_verbatim(tmp_path):
    command = [sys.executable, "-c", "pass"]
    client = CloudTranscriptClient(command=command, timeout=DEFAULT_TIMEOUT)
    assert list(client.command) == command
    assert client.timeout == DEFAULT_TIMEOUT


# -- transport ------------------------------------------------------


def test_fetch_returns_server_payload(server):
    command, _ = server("ok")
    client = CloudTranscriptClient(command=command, timeout=10)
    data = client.fetch({"tool": "get_transcripts", "arguments": {"sinceId": 5}})
    assert data["tool"] == "get_transcripts"
    assert data["resultCount"] == 0
    assert data["truncated"] is False
    assert data["echo"] == {"sinceId": 5}


def test_fetch_accepts_inline_arguments(server):
    command, _ = server("ok")
    client = CloudTranscriptClient(command=command, timeout=10)
    assert client.fetch({"tool": "get_transcripts", "sinceId": 7})["echo"] == {"sinceId": 7}


def test_nested_arguments_win_over_inline(server):
    command, _ = server("ok")
    client = CloudTranscriptClient(command=command, timeout=10)
    data = client.fetch({"tool": "get_transcripts", "sinceId": 7,
                          "arguments": {"sinceId": 9}})
    assert data["echo"] == {"sinceId": 9}


def test_fetch_requires_explicit_tool(server):
    command, _ = server("ok")
    client = CloudTranscriptClient(command=command, timeout=10)
    with pytest.raises(ValueError):
        client.fetch({})
    with pytest.raises(TypeError):
        client.fetch("get_transcripts")


def test_upstream_error_is_reported_without_payload(server):
    command, _ = server("error")
    client = CloudTranscriptClient(command=command, timeout=10)
    with pytest.raises(CloudTranscriptError) as excinfo:
        client.fetch({"tool": "get_transcripts"})
    message = str(excinfo.value)
    assert "sk_test_FAKE0123456789" not in message
    assert "example.invalid" not in message
    assert "get_transcripts" in message


def test_invalid_payload_is_reported(server):
    command, _ = server("badjson")
    client = CloudTranscriptClient(command=command, timeout=10)
    with pytest.raises(CloudTranscriptError):
        client.fetch({"tool": "get_transcripts"})


def test_missing_server_is_reported():
    client = CloudTranscriptClient(command=["definitely-not-a-real-binary"], timeout=5)
    with pytest.raises(CloudTranscriptError):
        client.fetch({"tool": "sync_status"})


def test_server_that_exits_is_reported(server):
    command, _ = server("exit")
    client = CloudTranscriptClient(command=command, timeout=10)
    with pytest.raises(CloudTranscriptError):
        client.fetch({"tool": "sync_status"})


def test_timeout_is_bounded(server):
    command, _ = server("hang")
    client = CloudTranscriptClient(command=command, timeout=1)
    started = time.monotonic()
    with pytest.raises(CloudTranscriptTimeout):
        client.fetch({"tool": "get_transcripts"})
    assert time.monotonic() - started < 20


def test_wedged_child_is_terminated(server):
    command, pid_file = server("wedge")
    client = CloudTranscriptClient(command=command, timeout=1)
    with pytest.raises(CloudTranscriptTimeout):
        client.fetch({"tool": "get_transcripts"})
    pid = _pid(pid_file)
    deadline = time.monotonic() + 5
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _running(pid), "wedged child was left running"


def test_child_is_reaped_after_success(server):
    command, pid_file = server("ok")
    client = CloudTranscriptClient(command=command, timeout=10)
    client.fetch({"tool": "sync_status"})
    pid = _pid(pid_file)
    deadline = time.monotonic() + 5
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _running(pid), "child was left running"


# -- normalization --------------------------------------------------

RECORD = {"sourceType": "transcript", "id": 42, "timeMs": 1730000000000,
          "title": "标题", "text": "这是原话", "device": "phone"}


def test_normalize_transcript_maps_verified_record():
    segment = normalize_transcript(RECORD, "phone")
    assert segment.segment_id == "cloud-transcript-sync:phone:42"
    assert segment.source == SOURCE
    assert segment.text == "这是原话"
    assert segment.timestamp_start == segment.timestamp_end
    assert segment.timestamp_start.tzinfo is not None
    assert segment.timestamp_start.utcoffset().total_seconds() == 0
    assert segment.timestamp_start.year == 2024


def test_normalize_transcript_is_stable_and_device_scoped():
    first = normalize_transcript(RECORD, "phone")
    again = normalize_transcript(RECORD, "phone")
    other = normalize_transcript(RECORD, "watch")
    assert first.segment_id == again.segment_id
    assert first.segment_id != other.segment_id


def test_normalize_transcript_keeps_device_identity_exact():
    segment = normalize_transcript(RECORD, " Phone ")
    assert segment.segment_id == "cloud-transcript-sync: Phone :42"
    assert normalize_transcript(RECORD).segment_id == "cloud-transcript-sync:phone:42"
    assert normalize_transcript({**RECORD, "device": ""}).segment_id == "cloud-transcript-sync::42"


def test_normalize_transcript_skips_empty_text():
    assert normalize_transcript({**RECORD, "text": ""}, "phone") is None
    assert normalize_transcript({**RECORD, "text": "   "}, "phone") is None
    assert normalize_transcript({**RECORD, "text": None}, "phone") is None


def test_normalize_transcript_rejects_invalid_numbers():
    with pytest.raises(ValueError):
        normalize_transcript({**RECORD, "id": "abc"}, "phone")
    with pytest.raises(ValueError):
        normalize_transcript({**RECORD, "id": True}, "phone")
    with pytest.raises(ValueError):
        normalize_transcript({**RECORD, "timeMs": None}, "phone")
    with pytest.raises(ValueError):
        normalize_transcript({**RECORD, "timeMs": "later"}, "phone")


def test_normalize_transcript_rejects_oversized_text():
    with pytest.raises(ValueError):
        normalize_transcript({**RECORD, "text": "x" * (MAX_TEXT + 1)}, "phone")
    segment = normalize_transcript({**RECORD, "text": "x" * MAX_TEXT}, "phone")
    assert segment.text == "x" * MAX_TEXT
