#!/usr/bin/env python3
"""A fake ``codex exec`` agent used by the decision-program tests.

The agent behaves the way the real one has to: it reads the prompt from
stdin, finds the absolute ``decide.py`` / ``yn.py`` command the prompt
offers and runs it.  Everything the agent does is driven by environment
variables, so no model, no network and no real CLI are involved.

``SAID_DONE_FAKE_SCENARIO``
    ``n``                  run ``decide.py N "<reason>"``
    ``n_no_reason``        run ``decide.py N ""``
    ``valid_submit``       write a valid draft and run ``decide.py submit``
    ``invalid_then_fixed`` submit an invalid draft, then the fixed one
    ``attempt_limit``      submit an invalid draft until the cap stops it
    ``chatty``             talk for a while, then run the valid submit
    ``never``              never run the program, only prose
    ``smoke_y``            run ``yn.py Y``
    ``smoke_n``            run ``yn.py N``
    ``smoke_never``        never run the smoke program
    ``final_message``      legacy mode: write the ``-o`` answer file
    ``cli_error``         fail with a JSON error that carries a code

``SAID_DONE_FAKE_INVALID``
    Which draft the agent rejects first: ``evidence`` (a quote that is not
    in the transcript), ``policy`` (a task type the gate forbids) or
    ``control`` (a cancel signal for an unknown task).

``SAID_DONE_FAKE_REASON``          the reason used by the ``n`` scenario
``SAID_DONE_FAKE_ARGV_FILE``       records the argv and the working directory
``SAID_DONE_FAKE_PROMPT_FILE``     records the prompt the agent received
``SAID_DONE_FAKE_CAPTURE_DIR``     keeps a copy of the program directory
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SCENARIO = os.environ.get('SAID_DONE_FAKE_SCENARIO', 'never')
INVALID_KIND = os.environ.get('SAID_DONE_FAKE_INVALID', 'evidence')
REASON = os.environ.get('SAID_DONE_FAKE_REASON') or '转写内容里没有可以执行的任务'
CAPTURE_DIR = os.environ.get('SAID_DONE_FAKE_CAPTURE_DIR')

PROSE = ('I looked at the transcript and the request seems actionable. ' * 12)

CAPTURED = ('decide.py', 'yn.py', 'context.json', 'schema.json', 'draft.json',
            'final.json', 'attempts.txt', 'answer.txt', 'output-schema.json',
            'last-message.txt')


def read_prompt() -> str:
    try:
        return sys.stdin.read() or ''
    except (OSError, ValueError):
        return ''


def commands(prompt: str) -> list[str]:
    """The commands the prompt offers, in backticks."""
    return re.findall(r'`([^`\n]+)`', prompt)


def split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def record(argv: list[str], prompt: str) -> None:
    target = os.environ.get('SAID_DONE_FAKE_ARGV_FILE')
    if target:
        Path(target).write_text(json.dumps({'argv': argv, 'cwd': os.getcwd()}),
                                encoding='utf-8')
    target = os.environ.get('SAID_DONE_FAKE_PROMPT_FILE')
    if target:
        Path(target).write_text(prompt, encoding='utf-8')


def workdir_from(argv: list[str], prompt: str) -> Path:
    if '-C' in argv:
        index = argv.index('-C')
        if index + 1 < len(argv):
            return Path(argv[index + 1])
    for command in commands(prompt):
        parts = split(command)
        if len(parts) > 1 and parts[1].endswith(('decide.py', 'yn.py')):
            return Path(parts[1]).resolve().parent
    return Path.cwd()


def load_context(workdir: Path) -> dict:
    try:
        return json.loads((workdir / 'context.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def draft_payload(context: dict, valid: bool) -> dict:
    """A draft the gate accepts, or one it must reject for a named reason."""
    segments = (context.get('batch') or {}).get('segments') or []
    segment = segments[0] if segments else {}
    segment_id = segment.get('segment_id') or 'seg-1'
    text = (segment.get('text') or '').strip()
    task = {
        'task_type': 'CREATE_DOCUMENT',
        'goal': '生成一份 XRD 原理报告大纲',
        'evidence_segment_ids': [segment_id],
        'original_evidence': [text[:40] or 'evidence'],
        'expected_outputs': ['xrd_outline.docx'],
        'dedup_key': 'fake-agent-key',
    }
    if not valid:
        if INVALID_KIND == 'policy':
            task['task_type'] = 'WRITE_CODE'
        elif INVALID_KIND == 'control':
            return {'decisions': [{'action': 'CANCEL', 'reason': '用户要求取消',
                                   'target_dedup_key': 'ghost-key-not-in-context'}]}
        else:
            task['original_evidence'] = ['这段引文并不存在于转写内容中']
    return {'decisions': [{'action': 'APPROVE', 'reason': '目标明确，可以执行', 'task': task}]}


def run_command(argv: list[str]) -> int:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f'fake agent could not run the program ({type(exc).__name__})\n')
        return 70
    sys.stdout.write(completed.stdout or '')
    sys.stderr.write(completed.stderr or '')
    sys.stdout.flush()
    sys.stderr.flush()
    return completed.returncode


def run_n(prompt: str, reason: str) -> int:
    for command in commands(prompt):
        parts = split(command)
        if len(parts) >= 3 and parts[1].endswith('decide.py') and parts[2] == 'N':
            return run_command(parts[:3] + [reason])
    sys.stderr.write('fake agent found no N command\n')
    return 70


def submit_parts(prompt: str) -> list[str] | None:
    for command in commands(prompt):
        parts = split(command)
        if len(parts) >= 4 and parts[1].endswith('decide.py') and parts[2] == 'submit':
            return parts
    return None


def run_submit(prompt: str, context: dict, valid: bool) -> int:
    parts = submit_parts(prompt)
    if not parts:
        sys.stderr.write('fake agent found no submit command\n')
        return 70
    Path(parts[3]).write_text(json.dumps(draft_payload(context, valid), ensure_ascii=False),
                              encoding='utf-8')
    return run_command(parts)


def run_smoke(prompt: str, letter: str) -> int:
    for command in commands(prompt):
        parts = split(command)
        if len(parts) >= 3 and parts[1].endswith('yn.py') and parts[2] in ('Y', 'N'):
            if parts[2] == letter:
                return run_command(parts)
    sys.stderr.write('fake agent found no yn.py command\n')
    return 70


def write_cli_error() -> int:
    """A CLI failure in the shape the subscription CLIs really use."""
    payload = {'error': {'message': 'PRIVATE transcript text must never leak',
                         'type': 'invalid_request_error',
                         'code': 'invalid_json_schema'}}
    sys.stderr.write('ERROR: ' + json.dumps(payload) + '\n')
    sys.stderr.flush()
    sys.stdout.write('the model could not be reached\n')
    sys.stdout.flush()
    return 3


def write_answer(argv: list[str]) -> int:
    answer = '{"decisions": []}'
    if '-o' in argv:
        index = argv.index('-o')
        if index + 1 < len(argv):
            Path(argv[index + 1]).write_text(answer, encoding='utf-8')
    sys.stdout.write(answer + '\n')
    return 0


def capture(workdir: Path) -> None:
    if not CAPTURE_DIR:
        return
    target = Path(CAPTURE_DIR)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in CAPTURED:
            source = workdir / name
            if source.is_file():
                shutil.copy2(source, target / name)
    except OSError:
        pass


def run(prompt: str, argv: list[str], workdir: Path, context: dict) -> int:
    if SCENARIO == 'final_message':
        return write_answer(argv)
    if SCENARIO == 'cli_error':
        return write_cli_error()
    if SCENARIO == 'never':
        sys.stdout.write(PROSE)
        return 0
    if SCENARIO == 'n':
        return run_n(prompt, REASON)
    if SCENARIO == 'n_no_reason':
        return run_n(prompt, '')
    if SCENARIO == 'valid_submit':
        return run_submit(prompt, context, True)
    if SCENARIO == 'invalid_then_fixed':
        code = run_submit(prompt, context, False)
        if code not in (0, 1):
            return code
        return run_submit(prompt, context, True)
    if SCENARIO == 'attempt_limit':
        code = 0
        for _ in range(6):
            code = run_submit(prompt, context, False)
        return code
    if SCENARIO == 'chatty':
        sys.stdout.write(PROSE)
        return run_submit(prompt, context, True)
    if SCENARIO == 'smoke_y':
        return run_smoke(prompt, 'Y')
    if SCENARIO == 'smoke_n':
        return run_smoke(prompt, 'N')
    if SCENARIO == 'smoke_never':
        sys.stdout.write(PROSE)
        return 0
    sys.stdout.write(PROSE)
    return 0


def main() -> int:
    prompt = read_prompt()
    argv = sys.argv[1:]
    record(argv, prompt)
    workdir = workdir_from(argv, prompt)
    try:
        return run(prompt, argv, workdir, load_context(workdir))
    finally:
        capture(workdir)


if __name__ == '__main__':
    raise SystemExit(main())
