"""Mock provider: the existing conservative classifier behind the provider API.

The mock is never a real model substitute; it only exists so the whole
pipeline can be exercised offline.  ``said_done.semantic.mock_adapter`` is imported
inside :meth:`complete` on purpose: the semantic package imports this package,
so a module level import would be a cycle.
"""
from __future__ import annotations

import json

from .base import ModelInfo, ProviderError


def batch_from_messages(messages: list[dict]):
    """Recover the transcript batch a gate sent in its messages."""
    from said_done.contracts import TranscriptBatch

    payload = None
    for message in reversed(messages or []):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if payload is not None:
            break
    if not isinstance(payload, dict):
        raise ProviderError('mock provider received no usable transcript batch', retryable=False,
                            error_type='InvalidInput')
    if 'transcript_batch' in payload:
        payload = payload['transcript_batch']
    try:
        return TranscriptBatch.model_validate(payload)
    except Exception:
        raise ProviderError('mock provider received an invalid transcript batch', retryable=False,
                            error_type='InvalidInput') from None


class MockProvider:
    kind = 'mock'

    def __init__(self, *, model: str | None = None, gate=None) -> None:
        self.model = model or 'mock'
        self.gate = gate

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=self.model, display_name='mock classifier', supports_effort=False)]

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        from said_done.semantic.mock_adapter import MockSemanticAdapter

        batch = batch_from_messages(messages)
        response = MockSemanticAdapter(self.gate).classify(batch)
        return response.model_dump_json()

    def close(self) -> None:
        """Nothing to release."""
