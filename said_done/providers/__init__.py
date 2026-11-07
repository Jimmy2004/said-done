"""Model providers for the screening classifier (docs/DESIGN.md §3).

Public API (docs/DESIGN.md §10)::

    said_done.providers.SUBSCRIPTION_DISCLAIMER: str
    said_done.providers.build_model_provider(settings) -> ModelProvider
    said_done.providers.list_models(settings) -> list[dict]
    said_done.providers.default_model_for_kind(kind) -> str | None

plus the contract itself (``ModelProvider``, ``ModelInfo``, ``ProviderError``)
so other packages can type against it.
"""
from __future__ import annotations

from .anthropic import AnthropicProvider
from .chat_completions import ChatCompletionsProvider
from .base import (API_STYLES, ModelInfo, ModelProvider, ProviderError,
                   SubscriptionRiskNotAcknowledged, normalize_effort, omit_none_effort)
from .claude_subscription import ClaudeSubscriptionProvider
from .codex_subscription import CodexSubscriptionProvider
from .disclaimer import (SUBSCRIPTION_DISCLAIMER, SUBSCRIPTION_DISCLAIMER_EN,
                         SUBSCRIPTION_DISCLAIMER_ZH)
from .factory import (build_model_provider, default_model_for_kind,
                      detect_api_style, list_models, resolve_api_style)
from .minimax import MiniMaxProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    'API_STYLES',
    'ChatCompletionsProvider',
    'AnthropicProvider',
    'CodexSubscriptionProvider',
    'ClaudeSubscriptionProvider',
    'MiniMaxProvider',
    'MockProvider',
    'ModelInfo',
    'ModelProvider',
    'OpenAICompatibleProvider',
    'ProviderError',
    'SUBSCRIPTION_DISCLAIMER',
    'SUBSCRIPTION_DISCLAIMER_EN',
    'SUBSCRIPTION_DISCLAIMER_ZH',
    'SubscriptionRiskNotAcknowledged',
    'build_model_provider',
    'default_model_for_kind',
    'detect_api_style',
    'list_models',
    'normalize_effort',
    'omit_none_effort',
    'resolve_api_style',
]
