"""OpenAI Responses API provider."""
from __future__ import annotations

from .base import HttpProvider, ModelInfo, ProviderError, omit_none_effort
from .chat_completions import ChatCompletionsProvider


class OpenAICompatibleProvider(HttpProvider):
    kind = 'openai-compatible'
    DEFAULT_BASE_URL = 'https://api.openai.com/v1'

    def __init__(self, *, api_key: str, base_url: str | None = None, **kwargs) -> None:
        super().__init__(api_key=api_key, base_url=base_url or self.DEFAULT_BASE_URL, **kwargs)

    def chat_url(self) -> str:
        return f'{self.base_url}/responses'

    def models_url(self) -> str:
        return f'{self.base_url}/models'

    def build_body(self, messages: list[dict], *, model: str, effort: str | None,
                   json_schema: dict | None, max_output_tokens: int) -> dict:
        body = {'model': model, 'input': list(messages),
                'max_output_tokens': int(max_output_tokens)}
        resolved = omit_none_effort(effort if effort is not None else self.effort)
        if resolved is not None:
            body['reasoning'] = {'effort': resolved}
        body.update(self.extra_body)
        return body

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        payload = self._post(self.chat_url(), self.build_body(
            messages, model=self.resolve_model(model), effort=effort,
            json_schema=json_schema, max_output_tokens=max_output_tokens))
        error = payload.get('error')
        if error is not None:
            # Only type/code metadata is retained, never provider message bodies.
            category = (error.get('type') or error.get('code')) if isinstance(error, dict) else None
            category = self._redact(category) if isinstance(category, str) else None
            raise ProviderError('Responses API returned an error', error_type=category or 'ProviderError')
        if payload.get('status') == 'incomplete':
            raise ProviderError('Responses API returned incomplete output', error_type='IncompleteResponse')
        text = payload.get('output_text')
        if not isinstance(text, str) or not text.strip():
            parts = []
            output = payload.get('output')
            for item in output if isinstance(output, list) else []:
                if not isinstance(item, dict) or item.get('type') != 'message':
                    continue
                content = item.get('content')
                for part in content if isinstance(content, list) else []:
                    if (isinstance(part, dict) and part.get('type') == 'output_text'
                            and isinstance(part.get('text'), str)):
                        parts.append(part['text'])
            text = ''.join(parts)
        if not text.strip():
            raise ProviderError('Responses API returned empty content', error_type='EmptyContent')
        return text.strip()

    def list_models(self) -> list[ModelInfo]:
        return ChatCompletionsProvider._list_models(self)
