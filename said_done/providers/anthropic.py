"""Anthropic messages provider (docs/DESIGN.md §3).

Wire format: ``POST {base_url}/v1/messages`` with ``x-api-key`` and
``anthropic-version`` headers, ``GET {base_url}/v1/models`` (paginated) for the
model list, and ``output_config.effort`` as the effort mapping.  ``extra_body``
is merged last, so it can override ``output_config``.
"""
from __future__ import annotations

from .base import HttpProvider, ModelInfo, ProviderError, omit_none_effort

ANTHROPIC_VERSION = '2023-06-01'


class AnthropicProvider(HttpProvider):
    kind = 'anthropic'
    DEFAULT_BASE_URL = 'https://api.anthropic.com'

    def __init__(self, *, api_key: str, base_url: str | None = None, **kwargs) -> None:
        super().__init__(api_key=api_key, base_url=(base_url or self.DEFAULT_BASE_URL), **kwargs)

    def _headers(self) -> dict[str, str]:
        return {'x-api-key': self.api_key.get_secret_value(), 'anthropic-version': ANTHROPIC_VERSION,
                'Content-Type': 'application/json'}

    def _api_path(self, suffix: str) -> str:
        """``{base}/v1/{suffix}`` without doubling an existing ``/v1``."""
        base = self.base_url
        if base.endswith('/v1'):
            return base + suffix
        return base + '/v1' + suffix

    def chat_url(self) -> str:
        return self._api_path('/messages')

    def models_url(self) -> str:
        return self._api_path('/models')

    def build_body(self, messages: list[dict], *, model: str, effort: str | None,
                   json_schema: dict | None, max_output_tokens: int) -> dict:
        system_parts = [str(m.get('content') or '') for m in messages if m.get('role') == 'system']
        conversation = [{'role': str(m.get('role') or 'user'), 'content': m.get('content')}
                        for m in messages if m.get('role') != 'system']
        body: dict = {
            'model': model,
            'messages': conversation,
            'max_tokens': int(max_output_tokens or 4096),
        }
        system = '\n\n'.join(part for part in system_parts if part)
        if system:
            body['system'] = system
        # Anthropic is the only provider that omits an explicit ``none``.
        resolved = omit_none_effort(effort if effort is not None else self.effort)
        if resolved is not None:
            body['output_config'] = {'effort': resolved}
        body.update(self.extra_body)
        return body

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        target = self.resolve_model(model)
        payload = self._post(self.chat_url(), self.build_body(
            messages, model=target, effort=effort, json_schema=json_schema,
            max_output_tokens=max_output_tokens))
        blocks = payload.get('content')
        if not isinstance(blocks, list):
            raise ProviderError('anthropic returned an unexpected body', retryable=True,
                                error_type='InvalidBody')
        texts = [block.get('text', '') for block in blocks
                 if isinstance(block, dict) and block.get('type') == 'text'
                 and isinstance(block.get('text'), str)]
        content = ''.join(texts).strip()
        if not content:
            raise ProviderError('anthropic returned empty content', retryable=True,
                                error_type='EmptyContent')
        return content

    def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        params: dict = {'limit': 100}
        seen_pages: set[str] = set()
        while True:
            payload = self._get(self.models_url(), params=params)
            items = payload.get('data')
            if not isinstance(items, list):
                raise ProviderError('anthropic returned an unexpected model list', retryable=True,
                                    error_type='InvalidModelList')
            for item in items:
                info = self._model_info(item)
                if info is not None:
                    models.append(info)
            last_id = payload.get('last_id')
            if not payload.get('has_more') or not isinstance(last_id, str) or last_id in seen_pages:
                break
            seen_pages.add(last_id)
            params = {'limit': 100, 'after_id': last_id}
        return models
