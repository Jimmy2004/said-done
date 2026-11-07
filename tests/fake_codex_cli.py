"""A fake ``codex exec`` used by the executor tests.

It writes the output files the prompt asks for and prints one JSON event, so
the CLI adapter can be exercised without a model or a network.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_app_server import write_docx  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    prompt = ""
    for arg in argv:
        if "Goal:" in arg:
            prompt = arg
            break
    last_message_path = None
    for index, arg in enumerate(argv):
        if arg == "-o" and index + 1 < len(argv):
            last_message_path = argv[index + 1]

    goal_match = re.search(r"^Goal: (.+)$", prompt, re.M)
    goal = goal_match.group(1).strip() if goal_match else "Report"
    if 'HANG' in goal:
        import time
        time.sleep(60)
    for line in re.findall(r"^Output file: (.+)$", prompt, re.M):
        target = Path(line.strip())
        if target.suffix.lower() == ".docx":
            write_docx(target, goal)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{goal}\n", encoding="utf-8")

    event = {"type": "item.completed", "item": {"type": "agentMessage", "text": f"produced: {goal}"}}
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
    if last_message_path:
        Path(last_message_path).write_text(f"produced: {goal}", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
