"""A fake Codex app-server used by the executor tests.

It speaks the same JSONL JSON-RPC dialect as the real server but never calls a
model: ``turn/start`` writes the output files the prompt asked for and reports
a completed turn.  No credentials, no inference, no network.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path

CONTENT_TYPES = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
    "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
    "<Default Extension='xml' ContentType='application/xml'/>"
    "<Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-"
    "officedocument.wordprocessingml.document.main+xml'/></Types>"
)

RELS = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
    "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships/officeDocument' Target='word/document.xml'/></Relationships>"
)


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def write_docx(path: Path, text: str) -> None:
    document = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        "<w:body>"
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Produced by the fake app-server for the executor tests.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document)


def probe_paths(script: str) -> list[str]:
    match = re.search(r"\$items = '(.*)'", script)
    if not match:
        return []
    try:
        return [str(item) for item in json.loads(match.group(1))]
    except json.JSONDecodeError:
        return []


def handle(message: dict) -> None:
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        send(
            {
                "id": mid,
                "result": {
                    "userAgent": "FakeCodex/0.1.0 (asr-agent tests)",
                    "codexHome": str(Path.home() / ".codex"),
                    "platformFamily": "windows",
                    "platformOs": "windows",
                },
            }
        )
        return

    if method == "thread/start":
        if params.get('ephemeral') is not True:
            send({'id':mid,'error':{'code':-32600,'message':'acceptance sessions must not persist in the sidebar'}})
            return
        cwd = params.get("cwd") or os.getcwd()
        send({"id": mid, "result": {"thread": {"id": "thread-1", "sessionId": "different-session-tree", "cwd": cwd}}})
        return

    if method == "command/exec":
        cwd = Path(params.get("cwd") or os.getcwd())
        if (cwd / "probe_should_fail").exists():
            send({"id": mid, "result": {"exitCode": 1, "stdout": "", "stderr": "sandbox denied"}})
            return
        script = ""
        command = params.get("command") or []
        if command:
            script = str(command[-1])
        lines = []
        for path in probe_paths(script):
            if Path(path).is_file():
                lines.append(f"OK|{path}")
            else:
                lines.append(f"FAIL|{path}|missing or not a file")
        if not lines:
            lines.append("OK|none")
        send({"id": mid, "result": {"exitCode": 0, "stdout": "\n".join(lines) + "\n", "stderr": ""}})
        return

    if method == "turn/start":
        thread_id = params.get("threadId", "thread-1")
        if thread_id != 'thread-1':
            send({'id':mid,'error':{'code':-32600,'message':'wrong thread identity'}});return
        turn_id = "turn-1"
        text = ""
        for item in params.get("input") or []:
            if item.get("type") == "text":
                text = item.get("text", "")
        send({"id": mid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        cwd=Path(params.get('cwd') or os.getcwd())
        if (cwd/'hang-turn').exists():return
        goal_match = re.search(r"^Goal: (.+)$", text, re.M)
        goal = goal_match.group(1).strip() if goal_match else "Report"
        for line in re.findall(r"^Output file: (.+)$", text, re.M):
            target = Path(line.strip())
            if target.suffix.lower() == ".docx":
                write_docx(target, goal)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"{goal}\n", encoding="utf-8")
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "completedAtMs": 1,
                    "item": {"id": "item-1", "type": "agentMessage", "text": f"produced: {goal}"},
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "mystery" if (cwd/'unknown-status').exists() else "completed", "items": []},
                },
            }
        )
        return

    if method == "turn/interrupt":
        send({"id": mid, "result": {}})
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": params.get("threadId", "thread-1"),
                    "turn": {"id": params.get("turnId", "turn-1"), "status": "interrupted", "items": []},
                },
            }
        )
        return

    if mid is not None:
        send({"id": mid, "result": {}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(message)


if __name__ == "__main__":
    main()
