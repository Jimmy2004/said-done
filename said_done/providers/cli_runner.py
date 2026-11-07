"""Bounded subprocess execution for the subscription CLIs (docs/DESIGN.md §3).

Every subscription call runs in a throw-away empty directory, with a timeout,
with the output size capped, and with the whole process group killed when the
run ends or overruns.  The bounded I/O and the group kill are reused from
``said_done.executor.process_control`` (``communicate_bounded`` / ``stop_process``)
so providers and executors share one implementation.

Stderr is never copied into an error message: a CLI failure is reported as the
tool name, the exception type and the exit code only.
"""
from __future__ import annotations
import json
import re

import os
import subprocess
from pathlib import Path

from said_done.executor.process_control import MAX_OUTPUT, communicate_bounded, stop_process

from .base import ProviderError

__all__ = ['CliResult', 'run_cli', 'MAX_OUTPUT']


class CliResult:
    """The outcome of one bounded CLI run."""

    __slots__ = ('stdout', 'stderr', 'returncode')

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def run_cli(argv: list[str], *, prompt: str, timeout: float, cwd: Path | None = None,
            limit: int = MAX_OUTPUT, tool: str = 'cli') -> CliResult:
    """Run ``argv`` with ``prompt`` on stdin and return its bounded output."""
    popen_kwargs: dict = {
        'stdin': subprocess.PIPE,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'text': True,
    }
    if os.name == 'nt':
        popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs['start_new_session'] = True
    try:
        process = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, **popen_kwargs)
    except FileNotFoundError:
        raise ProviderError(f'{tool} executable not found', retryable=False,
                            error_type='ExecutableNotFound') from None
    except OSError as exc:
        raise ProviderError(f'{tool} could not be started ({type(exc).__name__})', retryable=False,
                            error_type=type(exc).__name__) from None
    try:
        stdout, stderr = communicate_bounded(process, input=prompt, timeout=timeout, limit=limit)
    except subprocess.TimeoutExpired:
        stop_process(process)
        raise ProviderError(f'{tool} timed out after {timeout:g}s', retryable=True,
                            error_type='Timeout') from None
    except RuntimeError as exc:
        stop_process(process)
        raise ProviderError(f'{tool} produced more output than allowed ({type(exc).__name__})',
                            retryable=True, error_type='OutputLimit') from None
    except OSError as exc:
        stop_process(process)
        raise ProviderError(f'{tool} could not be driven ({type(exc).__name__})', retryable=True,
                            error_type=type(exc).__name__) from None
    return CliResult(stdout or '', stderr or '', int(process.returncode or 0))


_CODE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')


def cli_error_code(*streams: str) -> str | None:
    """Machine-readable error code from CLI output, never its message text.

    CLIs such as ``codex exec`` print upstream API errors as JSON
    (``{"error": {"code": "invalid_json_schema", "message": ...}}``). Only a
    short token-like ``code``/``type`` is returned, so prompt or transcript
    text quoted in the message can never leak into stored errors.
    """
    for stream in streams:
        for line in (stream or '').splitlines():
            start = line.find('{')
            if start < 0:
                continue
            try:
                payload = json.loads(line[start:])
            except ValueError:
                continue
            error = payload.get('error') if isinstance(payload, dict) else None
            if isinstance(error, dict):
                for key in ('code', 'type'):
                    value = error.get(key)
                    if isinstance(value, str) and _CODE.match(value):
                        return value
    return None
