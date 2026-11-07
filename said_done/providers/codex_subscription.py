"""Codex subscription provider (docs/DESIGN.md §3).

Uses the personal ChatGPT subscription through the official ``codex exec`` CLI
in a disposable directory. Program mode records a validated decision file;
legacy final-message mode reads the CLI answer through ``-o``. Prompts use stdin.

The CLI flags follow the published command line surface; they are exercised
only against a fake executable in the tests, never against the real binary.
"""
from __future__ import annotations

import json
import os
import stat as stat_module
import tempfile
import time
from typing import Literal
from pathlib import Path

from .base import (ModelInfo, ProviderError, SubscriptionRiskNotAcknowledged,
                   omit_none_effort)
from .cli_runner import MAX_OUTPUT, cli_error_code, run_cli
from .disclaimer import SUBSCRIPTION_DISCLAIMER

BUILTIN_MODELS = (
    ModelInfo(id='gpt-6-luna', display_name='GPT-6 Luna（最便宜，筛选默认）',
              supports_effort=True, recommended_for_screening=True),
    ModelInfo(id='default', display_name='default（用 CLI 默认）', supports_effort=True),
)


DEFAULT_MODEL_ALIAS = 'default'
# The cheapest model the ChatGPT subscription serves: screening is a light
# classification, so the cheap tier saves quota (docs/DESIGN.md §11).
DEFAULT_SCREENING_MODEL = 'gpt-6-luna'


def read_bounded_answer(path: Path, limit: int = MAX_OUTPUT) -> str:
    """Read the CLI answer file without trusting its size or its type.

    The file must be a regular file (a symlink, fifo or device is refused)
    and it may not be larger than the output limit.
    """
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        info = os.fstat(descriptor)
        if not stat_module.S_ISREG(info.st_mode):
            return ''
        if info.st_size > limit:
            raise ProviderError('codex CLI answer exceeds the allowed size', retryable=True,
                                error_type='OutputLimit')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ProviderError('codex CLI answer exceeds the allowed size', retryable=True,
                                error_type='OutputLimit')
        return data.decode('utf-8', errors='replace')
    except OSError:
        return ''
    finally:
        if descriptor is not None:
            os.close(descriptor)


def render_prompt(messages: list[dict]) -> str:
    """Flatten the chat messages into the single prompt a CLI expects."""
    parts = []
    for message in messages or []:
        content = message.get('content')
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return '\n\n'.join(parts)


class CodexSubscriptionProvider:
    kind = 'codex-subscription'
    supports_validation_context = True

    def __init__(self, *, acknowledge_subscription_risk: bool = False,
                 executable: str | None = None, model: str | None = None,
                 effort: str | None = None, timeout: float = 120,
                 extra_body: dict | None = None, use_output_schema: bool = False,
                 response_mode: Literal['program', 'final-message'] = 'program') -> None:
        if acknowledge_subscription_risk is not True:
            raise SubscriptionRiskNotAcknowledged(
                'subscription risk is not acknowledged: '
                'classifier.acknowledge_subscription_risk must be true\n'
                + SUBSCRIPTION_DISCLAIMER)
        self.executable = executable or os.getenv('SAID_DONE_CODEX_EXECUTABLE') or 'codex'
        if response_mode not in ('program', 'final-message'):
            raise ValueError('invalid response_mode')
        self.response_mode = response_mode
        self.model = model or DEFAULT_SCREENING_MODEL
        self.effort = effort
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})
        # Off by default: `--output-schema` is enforced by OpenAI strict
        # structured outputs, which rejects the gate schema (optional fields,
        # open `dict` objects) with HTTP 400 invalid_json_schema. The gate
        # already validates and retries, so the schema stays in the prompt.
        self.use_output_schema = use_output_schema

    # -- models ----------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """Built-in catalogue; the CLI also accepts any model id the user names."""
        return list(BUILTIN_MODELS)

    # -- request ---------------------------------------------------------
    def build_argv(self, *, model: str | None, effort: str | None,
                   schema_path: Path, output_path: Path) -> list[str]:
        argv = [self.executable, 'exec', '--sandbox', 'read-only', '--skip-git-repo-check',
                '--ephemeral']
        if self.use_output_schema:
            argv += ['--output-schema', str(schema_path)]
        argv += ['-o', str(output_path)]
        if model and model != DEFAULT_MODEL_ALIAS:
            argv += ['-m', model]
        resolved = omit_none_effort(effort if effort is not None else self.effort)
        if resolved:
            argv += ['-c', f'model_reasoning_effort="{resolved}"']
        argv.append('-')
        return argv

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int,
                 validation_context: dict | None = None) -> str:
        if self.response_mode == 'program' and validation_context is not None:
            return self._complete_program(messages, model=model, effort=effort,
                                          validation_context=validation_context)
        prompt = render_prompt(messages)
        with tempfile.TemporaryDirectory(prefix='said-done-codex-') as workspace:
            workdir = Path(workspace)
            schema_path = workdir / 'output-schema.json'
            output_path = workdir / 'last-message.txt'
            schema_path.write_text(json.dumps(json_schema or {'type': 'object'}, ensure_ascii=False),
                                   encoding='utf-8')
            argv = self.build_argv(model=(model or self.model or ''), effort=effort,
                                   schema_path=schema_path, output_path=output_path)
            result = run_cli(argv, prompt=prompt, timeout=self.timeout, cwd=workdir, tool='codex')
            if result.returncode != 0:
                code = cli_error_code(result.stderr, result.stdout)
                raise ProviderError(f'codex CLI exited with code {result.returncode}'
                                    + (f' ({code})' if code else ''), retryable=True,
                                    error_type='CliExit')
            text = read_bounded_answer(output_path)
            if not text.strip():
                # The answer file is the contract; stdout is only a fallback and
                # only when it is a JSON object, so CLI noise never becomes the
                # model answer.
                try:
                    fallback = json.loads(result.stdout or '')
                except ValueError:
                    fallback = None
                text = fallback if isinstance(fallback, str) else ''
            if not text.strip():
                raise ProviderError('codex CLI produced no answer', retryable=True,
                                    error_type='EmptyContent')
            return text.strip()

    def _program_argv(self, workdir: Path, model: str | None, effort: str | None) -> list[str]:
        argv = [self.executable, 'exec', '--sandbox', 'workspace-write', '-C', str(workdir),
                '--skip-git-repo-check', '--ephemeral']
        if model and model != DEFAULT_MODEL_ALIAS:
            argv += ['-m', model]
        resolved = omit_none_effort(effort if effort is not None else self.effort)
        if resolved:
            argv += ['-c', f'model_reasoning_effort="{resolved}"']
        return argv + ['-']

    def _complete_program(self, messages: list[dict], *, model: str, effort: str | None,
                          validation_context: dict) -> str:
        from .decision_program import create_decision_program, build_decision_prompt, read_final_answer
        with tempfile.TemporaryDirectory(prefix='said-done-decision-') as workspace:
            workdir = Path(workspace)
            create_decision_program(workdir, validation_context)
            result = run_cli(self._program_argv(workdir, model or self.model, effort),
                             prompt=build_decision_prompt(messages, workdir),
                             timeout=self.timeout, cwd=workdir, tool='codex')
            text = read_final_answer(workdir)
            if not text.strip():
                attempts = read_bounded_answer(workdir / 'attempts.txt', limit=64)
                if attempts.strip().isdigit() and int(attempts.strip()) >= 5:
                    raise ProviderError('decision attempt limit reached', retryable=True,
                                        error_type='DecisionAttemptLimit')
            if result.returncode != 0:
                code = cli_error_code(result.stderr, result.stdout)
                raise ProviderError(f'codex CLI exited with code {result.returncode}'
                                    + (f' ({code})' if code else ''), retryable=True,
                                    error_type='CliExit')
            if not text.strip():
                raise ProviderError('codex CLI produced no decision', retryable=True,
                                    error_type='NoDecision')
            return text.strip()

    def smoke_test(self) -> dict:
        from .decision_program import create_decision_program, build_smoke_prompt
        started = time.monotonic()
        answer = None
        error = None
        try:
            with tempfile.TemporaryDirectory(prefix='said-done-smoke-') as workspace:
                workdir = Path(workspace)
                create_decision_program(workdir, {})
                result = run_cli(self._program_argv(workdir, self.model, self.effort),
                                 prompt=build_smoke_prompt(workdir), timeout=self.timeout,
                                 cwd=workdir, tool='codex')
                value = read_bounded_answer(workdir / 'answer.txt')
                answer = value if value in ('Y', 'N') else None
                if result.returncode:
                    error = cli_error_code(result.stderr, result.stdout) or f'CliExit:{result.returncode}'
                elif answer != 'Y':
                    error = 'IncorrectAnswer' if answer == 'N' else 'NoAnswer'
        except Exception as exc:
            error = getattr(exc, 'error_type', type(exc).__name__)
        return {'ok': error is None and answer == 'Y', 'answer': answer,
                'seconds': time.monotonic() - started, 'error': error}

    def close(self) -> None:
        """The CLI is a short lived child process; nothing is kept open."""
