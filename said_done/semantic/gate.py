"""Provider independent task gate; only validated structured output can approve."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from said_done.config import GateConfig
from said_done.contracts import SemanticResponse, TranscriptBatch
if TYPE_CHECKING:
    from said_done.providers import ModelProvider

from .prompt import build_messages
from .validator import validate_response


class ClassificationError(RuntimeError):
    """A safe diagnostic which never includes model output or transcript text."""


def extract_json(content: str) -> dict:
    """Accept a JSON object, optionally enclosed by a single Markdown fence."""
    text = content.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != '```':
            raise ValueError('invalid JSON fence')
        text = '\n'.join(lines[1:-1])
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object')
    return data


class SemanticGate:
    def __init__(self, provider: ModelProvider, *, model: str, effort: str | None = None,
                 gate: GateConfig | None = None, max_attempts: int = 3,
                 context_provider: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not 1 <= max_attempts <= 5:
            raise ValueError('max_attempts must be 1..5')
        self.provider = provider
        self.model = model
        self.effort = effort
        self.gate = gate or GateConfig()
        self.max_attempts = max_attempts
        self.context_provider = context_provider
        self.sleep = sleep

    def classify(self, batch: TranscriptBatch) -> SemanticResponse:
        known_tasks = self.context_provider(batch) if self.context_provider else None
        messages = build_messages(batch, known_tasks, self.gate)
        last = 'invalid output'
        for attempt in range(self.max_attempts):
            try:
                extra = {}
                if getattr(self.provider, 'supports_validation_context', False):
                    extra['validation_context'] = {
                        'batch': batch.model_dump(mode='json'), 'known_tasks': known_tasks,
                        'gate': self.gate.model_dump(mode='json'),
                    }
                content = self.provider.complete(
                    messages, model=self.model, effort=self.effort,
                    json_schema=SemanticResponse.model_json_schema(), max_output_tokens=8192, **extra,
                )
                response = SemanticResponse.model_validate(extract_json(content))
                return validate_response(response, batch, known_tasks, self.gate)
            except Exception as exc:
                # Even validation exceptions can contain complete transcript excerpts.
                # Provider errors carry a safe category (RateLimited, CliExit...); keep it.
                category = getattr(exc, 'error_type', None)
                last = f'{type(exc).__name__}:{category}' if isinstance(category, str) and category else type(exc).__name__
                if getattr(exc, 'retryable', True) is False:
                    raise ClassificationError(f'classification rejected ({last})') from None
                if attempt + 1 < self.max_attempts:
                    messages = build_messages(batch, known_tasks, self.gate) + [{
                        'role': 'user',
                        'content': 'invalid output: 上次输出无效，请严格按Schema返回JSON，不得添加解释。',
                    }]
                    self.sleep(min(2 ** attempt, 8))
        raise ClassificationError(
            f'classification failed after {self.max_attempts} attempts ({last})'
        ) from None

    def close(self) -> None:
        self.provider.close()
