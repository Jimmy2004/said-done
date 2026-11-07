"""Chat Completions provider (docs/DESIGN.md §3).

Wire format: ``POST {base_url}/chat/completions`` with a Bearer token,
``GET {base_url}/models`` for the model list, and ``reasoning_effort`` as the
effort mapping (``None`` omits it; explicit ``none`` is sent).
"""
from __future__ import annotations

from .base import HttpProvider, ModelInfo, ProviderError, extract_message_text, normalize_effort, schema_name


class ChatCompletionsProvider(HttpProvider):
    kind = 'chat-completions'
    DEFAULT_BASE_URL = 'https://api.openai.com/v1'

    def __init__(self, *, api_key: str, base_url: str | None = None, use_response_format: bool = True, **kwargs) -> None:
        self.use_response_format = use_response_format
        self.model_list_note: str | None = None
        super().__init__(api_key=api_key, base_url=(base_url or self.DEFAULT_BASE_URL), **kwargs)

    # -- URLs ------------------------------------------------------------
    def chat_url(self) -> str:
        return f'{self.base_url}/chat/completions'

    def models_url(self) -> str:
        return f'{self.base_url}/models'

    # -- request ---------------------------------------------------------
    def build_body(self, messages: list[dict], *, model: str, effort: str | None,
                   json_schema: dict | None, max_output_tokens: int) -> dict:
        body: dict = {'model': model, 'messages': list(messages), 'stream': False}
        if max_output_tokens:
            body['max_tokens'] = int(max_output_tokens)
        # OpenAI compatible providers: ``None`` omits the field, an explicit
        # ``none`` is sent on purpose so the provider disables reasoning.
        resolved = normalize_effort(effort if effort is not None else self.effort)
        if resolved is not None:
            body['reasoning_effort'] = resolved
        if json_schema and self.use_response_format:
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': schema_name(json_schema), 'schema': json_schema},
            }
        body.update(self.extra_body)
        return body

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        target = self.resolve_model(model)
        payload = self._post(self.chat_url(), self.build_body(
            messages, model=target, effort=effort, json_schema=json_schema,
            max_output_tokens=max_output_tokens))
        return extract_message_text(payload)

    # -- models ----------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        self.model_list_note = None
        try:
            return self._list_models()
        except ProviderError:
            self.model_list_note = 'Model list unavailable; the user must supply the model ID.'
            import logging
            logging.getLogger(__name__).warning(self.model_list_note)
            return []

    def _list_models(self) -> list[ModelInfo]:
        payload = self._get(self.models_url())
        items = payload.get('data')
        if not isinstance(items, list):
            raise ProviderError(f'{self.kind} returned an unexpected model list', retryable=True,
                                error_type='InvalidModelList')
        models: list[ModelInfo] = []
        for item in items:
            info = self._model_info(item)
            if info is not None:
                models.append(info)
        return models
