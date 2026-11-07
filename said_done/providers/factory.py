"""Provider factory (contract: docs/DESIGN.md §3 and §10).

``build_model_provider(settings)`` maps ``settings.classifier`` onto one
concrete provider; ``list_models(settings)`` answers with the models the
configured provider can serve.  The API style is auto detected from the base
url when the configuration does not name one.
"""
from __future__ import annotations

import os

from .anthropic import AnthropicProvider
from .chat_completions import ChatCompletionsProvider
from .base import API_STYLES, ProviderError
from .claude_subscription import (ClaudeSubscriptionProvider,
                                 DEFAULT_SCREENING_MODEL as CLAUDE_SCREENING_MODEL)
from .codex_subscription import (CodexSubscriptionProvider,
                                DEFAULT_SCREENING_MODEL as CODEX_SCREENING_MODEL)
from .minimax import MiniMaxProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

# The cheapest screening model per provider kind (docs/DESIGN.md §11).  Only
# the two subscriptions have one: every API provider needs a model the user
# picked, and executors never borrow a screening model.
SCREENING_DEFAULT_MODELS = {
    'codex-subscription': CODEX_SCREENING_MODEL,
    'claude-subscription': CLAUDE_SCREENING_MODEL,
}


def default_model_for_kind(kind: str | None) -> str | None:
    """The cheapest screening model a provider kind falls back to.

    ``None`` means the kind has no built-in default: the agent has to list the
    models and let the user choose one.
    """
    return SCREENING_DEFAULT_MODELS.get(str(kind or ''))


def detect_api_style(base_url: str | None) -> str:
    from said_done.credentials.parsing import detect_api_style as detect
    return detect(base_url)


def resolve_api_style(config) -> str:
    """The effective style for a classifier configuration.

    An explicit ``api_style`` wins, then the configured ``kind`` when it names
    a concrete API style, and otherwise the base url decides.
    """
    explicit = getattr(config, 'api_style', None)
    if isinstance(explicit, str) and explicit.strip():
        style = explicit.strip().lower()
        if style in API_STYLES:
            return style
    kind = getattr(config, 'kind', 'openai-compatible')
    if kind in (*API_STYLES, 'minimax'):
        return kind
    return detect_api_style(getattr(config, 'base_url', None))


def _env_var_name(profile: str) -> str:
    try:
        from said_done.credentials.paths import env_var_name
        return env_var_name(profile)
    except Exception:
        return 'SAID_DONE_API_KEY_' + str(profile).upper().replace('-', '_')


def resolve_api_key(settings, profile: str = 'default') -> str | None:
    """Find the API key for ``profile``.

    Order: the ``SAID_DONE_API_KEY_<PROFILE>`` environment override, the system
    keyring through ``said_done.credentials``, and finally the legacy
    ``MINIMAX_API_KEY`` variable for a MiniMax classifier.
    """
    override = os.getenv(_env_var_name(profile))
    if override and override.strip():
        return override.strip()
    try:
        from said_done.credentials import get_api_key
    except Exception:
        get_api_key = None
    if get_api_key is not None:
        try:
            secret = get_api_key(settings, profile)
        except Exception:
            secret = None
        if secret is not None:
            text = secret.get_secret_value() if hasattr(secret, 'get_secret_value') else str(secret)
            if text and text.strip():
                return text.strip()
    if getattr(getattr(settings, 'classifier', None), 'kind', '') == 'minimax':
        legacy = os.getenv('MINIMAX_API_KEY')
        if legacy and legacy.strip():
            return legacy.strip()
    return None


def build_model_provider(settings, *, api_key: str | None = None, client=None):
    """Build the ``ModelProvider`` described by ``settings.classifier``."""
    config = settings.classifier
    kind = getattr(config, 'kind', 'mock')
    if kind == 'mock':
        return MockProvider(model=config.model, gate=settings.screening.gate)
    if kind == 'codex-subscription':
        return CodexSubscriptionProvider(
            acknowledge_subscription_risk=config.acknowledge_subscription_risk,
            model=config.model, effort=config.effort, timeout=config.timeout_seconds,
            extra_body=config.extra_body,
            use_output_schema=config.use_response_format,
            response_mode=config.cli_response_mode)
    if kind == 'claude-subscription':
        return ClaudeSubscriptionProvider(
            acknowledge_subscription_risk=config.acknowledge_subscription_risk,
            model=config.model, effort=config.effort, timeout=config.timeout_seconds,
            extra_body=config.extra_body)
    style = resolve_api_style(config)
    key = api_key or resolve_api_key(settings, getattr(config, 'profile', 'default'))
    if not key:
        raise ProviderError(
            f'no API key configured for profile {getattr(config, "profile", "default")!r}',
            retryable=False, error_type='MissingCredential')
    shared = {
        'api_key': key,
        'base_url': config.base_url,
        'timeout': config.timeout_seconds,
        'client': client,
        'extra_body': config.extra_body,
        'model': config.model,
        'effort': config.effort,
    }
    if style == 'anthropic':
        return AnthropicProvider(**shared)
    if style == 'minimax':
        return MiniMaxProvider(**shared)
    if style == 'chat-completions':
        return ChatCompletionsProvider(**shared, use_response_format=config.use_response_format)
    return OpenAICompatibleProvider(**shared)


def list_models(settings) -> list[dict]:
    """The models the configured provider can serve, as plain dictionaries.

    Every entry carries ``recommended_for_screening``: it is ``True`` for the
    cheapest model of the two subscriptions and ``None`` for every API
    provider, whose catalogue is not ranked, so the setup agent asks the user.
    """
    provider = build_model_provider(settings)
    try:
        models = provider.list_models()
    finally:
        provider.close()
    return [model.to_dict() for model in models]
