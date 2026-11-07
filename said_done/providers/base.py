"""Provider contract for the screening classifier (docs/DESIGN.md §3).

The gate (``said_done.semantic.gate``) only ever talks to a ``ModelProvider``; it
never knows the wire format of a provider.  A provider reports failures as
``ProviderError`` so the gate can decide whether a failure is worth another
attempt: ``retryable is False`` means permanent (bad credentials, a refused
subscription, a permanent provider business status) and the gate must stop
immediately, ``True`` means a transport or invalid-output failure that the
gate may retry up to ``classifier.max_attempts``.

No provider message may contain the API key, the transcript text or the raw
stderr of a CLI.  The API key is kept in a ``SecretStr`` and is unwrapped only
where a request header needs it.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

EFFORT_NONE = 'none'
API_STYLES = ('openai-compatible', 'anthropic', 'chat-completions')


class ProviderError(RuntimeError):
    """A provider call failed.

    Attributes:
        retryable: ``False`` for permanent failures (never retry), ``True``
            for transport level or invalid-output failures (retry allowed).
        status_code: HTTP status when the failure came from an HTTP call.
        business_code: provider specific business status code.
        error_type: a safe, non-sensitive classification of the failure.
    """

    def __init__(self, message: str, *, retryable: bool = True,
                 status_code: int | None = None, business_code: int | None = None,
                 error_type: str | None = None) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.business_code = business_code
        self.error_type = error_type


class SubscriptionRiskNotAcknowledged(ProviderError):
    """A subscription CLI was requested without an explicit risk acknowledgement."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, error_type='SubscriptionRiskNotAcknowledged')


class ModelInfo(BaseModel):
    """One model offered by a provider.

    ``recommended_for_screening`` marks the cheapest model of a subscription
    catalogue.  It stays ``None`` for every API provider, whose offerings vary
    too much to rank, so the setup agent asks the user there.
    """

    model_config = ConfigDict(extra='forbid')

    id: str
    display_name: str | None = None
    supports_effort: bool | None = None
    recommended_for_screening: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


@runtime_checkable
class ModelProvider(Protocol):
    """The only interface ``said_done.semantic.gate`` depends on."""

    kind: str

    def list_models(self) -> list[ModelInfo]:
        """Models this provider can serve, most useful first."""

    def complete(self, messages: list[dict], *, model: str, effort: str | None,
                 json_schema: dict | None, max_output_tokens: int) -> str:
        """Return the raw assistant text for ``messages``."""

    def close(self) -> None:
        """Release sockets, files and subprocesses owned by this provider."""


def normalize_effort(effort: str | None) -> str | None:
    """Return the effort value to send, or ``None`` when nothing was given.

    The literal ``none`` is kept: an OpenAI compatible provider sends
    ``reasoning_effort: "none"`` on purpose.  Anthropic and Responses drop it.
    """
    if effort is None:
        return None
    value = str(effort).strip()
    return value or None


def omit_none_effort(effort: str | None) -> str | None:
    """Return the effort to send, dropping the literal ``none`` as well."""
    value = normalize_effort(effort)
    if value is None or value.casefold() == EFFORT_NONE:
        return None
    return value


def schema_name(schema: dict | None) -> str:
    """A stable ``response_format.json_schema.name`` for a JSON schema."""
    title = ''
    if isinstance(schema, dict):
        title = str(schema.get('title') or '')
    cleaned = ''.join(ch for ch in title if ch.isalnum() or ch in '_-')
    return cleaned[:64] or 'response'


def extract_message_text(payload: dict) -> str:
    """Pull the assistant text out of an OpenAI style completion payload."""
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        raise ProviderError('provider returned no choices', retryable=True, error_type='EmptyPayload')
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError('provider returned an unexpected choice', retryable=True, error_type='EmptyPayload')
    message = first.get('message')
    content = message.get('content') if isinstance(message, dict) else first.get('text')
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') in (None, 'text'):
                text = block.get('text')
                if isinstance(text, str):
                    parts.append(text)
        content = ''.join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError('provider returned empty content', retryable=True, error_type='EmptyContent')
    return content.strip()


class HttpProvider:
    """Shared plumbing for the HTTP based providers.

    The API key lives in a ``SecretStr`` and is unwrapped only inside
    ``_headers``.  Every failure is re-raised as a freshly built
    ``ProviderError`` with the original exception dropped (``from None``),
    because an ``httpx`` exception carries the request, and therefore the key.
    """

    kind = 'http'
    default_model: str | None = None

    def __init__(self, *, api_key: str, base_url: str, timeout: float = 60,
                 client: httpx.Client | None = None, extra_body: dict | None = None,
                 model: str | None = None, effort: str | None = None) -> None:
        if not api_key or not api_key.strip():
            raise ProviderError(f'{self.kind}: no API key configured', retryable=False,
                                error_type='MissingCredential')
        self.api_key = SecretStr(api_key.strip())
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})
        self.model = model
        self.effort = effort
        self._owns_client = client is None
        self._http = client if client is not None else httpx.Client(timeout=timeout)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            try:
                self._http.close()
            except Exception:
                pass

    # -- helpers ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.api_key.get_secret_value()}',
                'Content-Type': 'application/json'}

    def resolve_model(self, model: str | None) -> str:
        candidate = (model or '').strip()
        if not candidate or candidate == 'default':
            candidate = (self.model or '').strip()
        if not candidate and self.default_model:
            candidate = self.default_model
        if not candidate:
            raise ProviderError(f'{self.kind}: no model configured', retryable=False,
                                error_type='MissingModel')
        return candidate

    def _redact(self, value: str | None) -> str | None:
        """Strip the API key from a value a server echoed back to us."""
        if not value:
            return None
        secret = self.api_key.get_secret_value()
        if secret and secret in value:
            value = value.replace(secret, '[redacted]')
        return value

    def _post(self, url: str, body: dict) -> dict:
        try:
            response = self._http.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f'{self.kind} request failed ({type(exc).__name__})', retryable=True,
                                error_type=type(exc).__name__) from None
        return self._read_json(response)

    def _get(self, url: str, params: dict | None = None) -> dict:
        try:
            response = self._http.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f'{self.kind} request failed ({type(exc).__name__})', retryable=True,
                                error_type=type(exc).__name__) from None
        return self._read_json(response)

    def _read_json(self, response: httpx.Response) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise ProviderError(f'{self.kind} authentication rejected (http {status})', retryable=False,
                                status_code=status, error_type='AuthRejected')
        if status == 408:
            raise ProviderError(f'{self.kind} request timed out (http {status})', retryable=True,
                                status_code=status, error_type='Timeout')
        if status == 429:
            raise ProviderError(f'{self.kind} rate limited (http {status})', retryable=True,
                                status_code=status, error_type='RateLimited')
        if status >= 500:
            raise ProviderError(f'{self.kind} server error (http {status})', retryable=True,
                                status_code=status, error_type='ServerError')
        if status >= 400:
            raise ProviderError(f'{self.kind} request rejected (http {status})', retryable=False,
                                status_code=status, error_type='BadRequest')
        try:
            data = response.json()
        except ValueError:
            raise ProviderError(f'{self.kind} returned a non JSON body', retryable=True,
                                status_code=status, error_type='InvalidBody') from None
        if not isinstance(data, dict):
            raise ProviderError(f'{self.kind} returned an unexpected body', retryable=True,
                                status_code=status, error_type='InvalidBody') from None
        return data

    def _model_info(self, item: dict) -> ModelInfo | None:
        """Build a public summary of one model; never echo the API key back."""
        if not isinstance(item, dict):
            return None
        identifier = item.get('id')
        if not isinstance(identifier, str) or not identifier.strip():
            return None
        identifier = self._redact(identifier.strip()) or ''
        if not identifier:
            return None
        display = item.get('display_name') or item.get('name') or item.get('owned_by') or None
        if not isinstance(display, str):
            display = None
        else:
            display = self._redact(display)
        return ModelInfo(id=identifier, display_name=display, supports_effort=None)
