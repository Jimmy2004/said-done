"""MiniMax provider: an OpenAI compatible preset with MiniMax business codes.

Wire format follows ``said_done.semantic.minimax_adapter`` (kept intact for the old
code path): the modern models use ``/v1/chat/completions`` with
``thinking={"type": "adaptive"}``, the older models use
``/v1/text/chatcompletion_v2``.  MiniMax answers HTTP 200 with a business
status inside ``base_resp.status_code``; 1004 / 1008 / 2049 / 2056 mean
credentials, balance or quota problems and are never retried, every other non
zero status is a transient business failure the gate may retry.
"""
from __future__ import annotations

from .base import ModelInfo, ProviderError, normalize_effort, schema_name
from .chat_completions import ChatCompletionsProvider

MODERN_MODELS = frozenset({'MiniMax-M3', 'MiniMax-M3.1-Flash'})
PERMANENT_BUSINESS_CODES = frozenset({1004, 1008, 2049, 2056})

BUILTIN_MODELS = (
    ModelInfo(id='MiniMax-M3.1-Flash', display_name='MiniMax M3.1 Flash', supports_effort=True),
    ModelInfo(id='MiniMax-M3', display_name='MiniMax M3', supports_effort=True),
    ModelInfo(id='MiniMax-M2.7', display_name='MiniMax M2.7', supports_effort=None),
    ModelInfo(id='MiniMax-M2.5', display_name='MiniMax M2.5', supports_effort=None),
    ModelInfo(id='MiniMax-M1', display_name='MiniMax M1', supports_effort=None),
)


class MiniMaxProvider(ChatCompletionsProvider):
    kind = 'minimax'
    DEFAULT_BASE_URL = 'https://api.minimaxi.com/v1'
    default_model = 'MiniMax-M3.1-Flash'

    # -- URLs ------------------------------------------------------------
    def chat_url(self, model: str | None = None) -> str:
        base = self.base_url
        modern = '/chat/completions' if base.endswith('/v1') else '/v1/chat/completions'
        legacy = '/text/chatcompletion_v2' if base.endswith('/v1') else '/v1/text/chatcompletion_v2'
        target = model or self.model or ''
        return base + (modern if target in MODERN_MODELS else legacy)

    # -- effort ----------------------------------------------------------
    def resolve_effort(self, model: str, effort: str | None) -> str | None:
        """Explicit effort wins; otherwise the historical M3.1 Flash default."""
        if effort is None:
            return 'low' if model == 'MiniMax-M3.1-Flash' else None
        return normalize_effort(effort)  # an explicit 'none' is sent as-is

    # -- request ---------------------------------------------------------
    def build_body(self, messages: list[dict], *, model: str, effort: str | None,
                   json_schema: dict | None, max_output_tokens: int) -> dict:
        body: dict = {'model': model, 'messages': list(messages), 'stream': False,
                      'max_completion_tokens': int(max_output_tokens or 8192)}
        if model in MODERN_MODELS:
            body['thinking'] = {'type': 'adaptive'}
        resolved = self.resolve_effort(model, effort if effort is not None else self.effort)
        if resolved is not None:
            body['reasoning_effort'] = resolved
        if json_schema:
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': schema_name(json_schema), 'schema': json_schema},
            }
        body.update(self.extra_body)
        return body

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        target = self.resolve_model(model)
        payload = self._post(self.chat_url(target), self.build_body(
            messages, model=target, effort=effort, json_schema=json_schema,
            max_output_tokens=max_output_tokens))
        self._check_business_status(payload)
        from .base import extract_message_text
        return extract_message_text(payload)

    def _check_business_status(self, payload: dict) -> None:
        """Read the MiniMax business status; a malformed one is never ignored.

        ``base_resp.status_code`` must be an integer.  A value of another type
        means the answer cannot be trusted, so it is reported as an invalid
        payload the gate may retry - it must not silently pass as success.
        """
        base_resp = payload.get('base_resp')
        if not isinstance(base_resp, dict) or 'status_code' not in base_resp:
            return
        code = base_resp.get('status_code')
        if code is None:
            return
        if isinstance(code, bool) or not isinstance(code, int):
            raise ProviderError('minimax returned an invalid business status', retryable=True,
                                error_type='InvalidPayload')
        if code == 0:
            return
        if code in PERMANENT_BUSINESS_CODES:
            raise ProviderError(f'minimax credential/balance/quota error {code}', retryable=False,
                                business_code=code, error_type='BusinessStatus')
        raise ProviderError(f'minimax business status {code}', retryable=True,
                            business_code=code, error_type='BusinessStatus')

    # -- models ----------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        try:
            return self._list_models()
        except ProviderError:
            # The MiniMax model list is not always exposed; fall back to the
            # built-in catalogue instead of failing the setup.
            return list(BUILTIN_MODELS)
