"""Claude subscription provider (docs/DESIGN.md §3).

Uses the personal Claude subscription through the official ``claude -p`` CLI in
a throw-away empty directory.  Classification must never execute anything, so
every tool is disabled and any inherited MCP tool configuration is dropped:
``--tools ''`` plus ``--strict-mcp-config --mcp-config '{"mcpServers":{}}'``.
The prompt goes through stdin and the answer is the ``result`` field of the
JSON output.

The CLI flags follow the published command line surface; they are exercised
only against a fake executable in the tests, never against the real binary.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .base import (ModelInfo, ProviderError, SubscriptionRiskNotAcknowledged,
                   omit_none_effort)
from .cli_runner import cli_error_code, run_cli
from .codex_subscription import render_prompt
from .disclaimer import SUBSCRIPTION_DISCLAIMER

BUILTIN_MODELS = (
    ModelInfo(id='claude-haiku-5-5', display_name='Haiku 5.5（最便宜，筛选默认）',
              supports_effort=True, recommended_for_screening=True),
    ModelInfo(id='default', display_name='default（用 CLI 默认）', supports_effort=True),
    ModelInfo(id='opus', display_name='Claude Opus', supports_effort=True),
    ModelInfo(id='sonnet', display_name='Claude Sonnet', supports_effort=True),
    ModelInfo(id='haiku', display_name='Claude Haiku', supports_effort=True),
)

# The cheapest model the Claude subscription serves: screening is a light
# classification, so the cheap tier saves quota (docs/DESIGN.md §11).
DEFAULT_SCREENING_MODEL = 'claude-haiku-5-5'

EMPTY_MCP_CONFIG = json.dumps({'mcpServers': {}}, separators=(',', ':'))


class ClaudeSubscriptionProvider:
    kind = 'claude-subscription'

    def __init__(self, *, acknowledge_subscription_risk: bool = False,
                 executable: str | None = None, model: str | None = None,
                 effort: str | None = None, timeout: float = 120,
                 extra_body: dict | None = None) -> None:
        if acknowledge_subscription_risk is not True:
            raise SubscriptionRiskNotAcknowledged(
                'subscription risk is not acknowledged: '
                'classifier.acknowledge_subscription_risk must be true\n'
                + SUBSCRIPTION_DISCLAIMER)
        self.executable = executable or os.getenv('SAID_DONE_CLAUDE_EXECUTABLE') or 'claude'
        self.model = model or DEFAULT_SCREENING_MODEL
        self.effort = effort
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})

    # -- models ----------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """Built-in aliases; the CLI also accepts any model id the user names."""
        return list(BUILTIN_MODELS)

    # -- request ---------------------------------------------------------
    def build_argv(self, *, model: str | None, effort: str | None) -> list[str]:
        argv = [self.executable, '-p', '--output-format', 'json']
        if model and model != 'default':
            argv += ['--model', model]
        resolved = omit_none_effort(effort if effort is not None else self.effort)
        if resolved:
            argv += ['--effort', resolved]
        # Classification must never execute anything: no tool is allowed and
        # no MCP server may be inherited from the user or the project config.
        # These flags are not optional, there is no way to weaken them.
        argv += ['--tools', '']
        argv += ['--strict-mcp-config', '--mcp-config', EMPTY_MCP_CONFIG]
        return argv

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        prompt = render_prompt(messages)
        with tempfile.TemporaryDirectory(prefix='said-done-claude-') as workspace:
            argv = self.build_argv(model=(model or self.model or ''), effort=effort)
            result = run_cli(argv, prompt=prompt, timeout=self.timeout, cwd=Path(workspace),
                             tool='claude')
            if result.returncode != 0:
                code = cli_error_code(result.stdout, result.stderr)
                raise ProviderError(f'claude CLI exited with code {result.returncode}'
                                    + (f' ({code})' if code else ''), retryable=True,
                                    error_type='CliExit')
            try:
                payload = json.loads(result.stdout or '{}')
            except ValueError:
                raise ProviderError('claude CLI returned a non JSON body', retryable=True,
                                    error_type='InvalidBody') from None
            if not isinstance(payload, dict):
                raise ProviderError('claude CLI returned an unexpected body', retryable=True,
                                    error_type='InvalidBody') from None
            if payload.get('is_error'):
                raise ProviderError('claude CLI reported an error result', retryable=False,
                                    error_type='CliReportedError')
            text = payload.get('result')
            if not isinstance(text, str) or not text.strip():
                raise ProviderError('claude CLI returned no answer', retryable=True,
                                    error_type='EmptyContent')
            return text.strip()

    def close(self) -> None:
        """The CLI is a short lived child process; nothing is kept open."""
