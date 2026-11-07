"""HTTP provider tests: request shape, effort mapping, model lists, errors.

No network and no credentials: every call is served by ``httpx.MockTransport``.
"""
import json

import httpx
import pytest

from said_done.providers import (AnthropicProvider, MiniMaxProvider, ChatCompletionsProvider,
                           ProviderError, detect_api_style)
from said_done.providers.base import normalize_effort, omit_none_effort

API_KEY = 'test-only-key'


def build(cls, handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault('base_url', 'https://api.example.com/v1')
    return cls(api_key=API_KEY, client=client, **kwargs)


def recorder(payload, status=200):
    seen = {}

    def handler(request):
        seen['url'] = str(request.url)
        seen['path'] = request.url.path
        seen['headers'] = dict(request.headers)
        seen['body'] = json.loads(request.content) if request.content else None
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(status, json=payload)

    return seen, handler


ANSWER = {'choices': [{'message': {'content': '{"decisions": []}'}}]}


# ---------------------------------------------------------------- openai ----
def test_openai_request_shape_headers_and_schema():
    seen, handler = recorder(ANSWER)
    provider = build(ChatCompletionsProvider, handler)
    out = provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort='low',
                            json_schema={'title': 'TaskGate', 'type': 'object'},
                            max_output_tokens=100)
    assert out == '{"decisions": []}'
    assert seen['path'] == '/v1/chat/completions'
    assert seen['headers']['authorization'] == f'Bearer {API_KEY}'
    assert seen['body']['model'] == 'gpt-x'
    assert seen['body']['max_tokens'] == 100
    assert seen['body']['reasoning_effort'] == 'low'
    assert seen['body']['response_format']['json_schema']['name'] == 'TaskGate'


@pytest.mark.parametrize('effort,expected', [(None, None), ('low', 'low'), ('none', 'none'),
                                              ('high', 'high')])
def test_openai_effort_semantics(effort, expected):
    """``None`` omits the field; an explicit ``none`` is sent on purpose."""
    seen, handler = recorder(ANSWER)
    provider = build(ChatCompletionsProvider, handler)
    provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort=effort,
                       json_schema=None, max_output_tokens=0)
    if expected is None:
        assert 'reasoning_effort' not in seen['body']
    else:
        assert seen['body']['reasoning_effort'] == expected


def test_openai_extra_body_is_merged_last():
    seen, handler = recorder(ANSWER)
    provider = build(ChatCompletionsProvider, handler, extra_body={'reasoning_effort': 'xhigh',
                                                                    'vendor_flag': True})
    provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort='low',
                       json_schema=None, max_output_tokens=10)
    assert seen['body']['reasoning_effort'] == 'xhigh'
    assert seen['body']['vendor_flag'] is True


def test_openai_list_models_and_redaction_of_the_key():
    payload = {'data': [{'id': 'gpt-x', 'display_name': 'GPT X'},
                        {'id': 'gpt-y', 'display_name': f'mirror of {API_KEY}'}]}
    seen, handler = recorder(payload)
    provider = build(ChatCompletionsProvider, handler)
    models = provider.list_models()
    assert [m.id for m in models] == ['gpt-x', 'gpt-y']
    assert models[0].display_name == 'GPT X'
    assert API_KEY not in (models[1].display_name or '')
    assert models[1].display_name == 'mirror of [redacted]'
    assert seen['path'] == '/v1/models'


def test_openai_empty_content_is_retryable():
    _, handler = recorder({'choices': [{'message': {'content': '  '}}]})
    provider = build(ChatCompletionsProvider, handler)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort=None,
                           json_schema=None, max_output_tokens=10)
    assert error.value.retryable is True


# -------------------------------------------------------------- anthropic ----
def test_anthropic_request_shape_and_effort():
    seen, handler = recorder({'content': [{'type': 'text', 'text': '{"decisions": []}'}]})
    provider = build(AnthropicProvider, handler, base_url='https://api.anthropic.com')
    out = provider.complete([{'role': 'system', 'content': 'rules'},
                             {'role': 'user', 'content': 'hi'}],
                            model='claude-x', effort='low', json_schema=None, max_output_tokens=64)
    assert out == '{"decisions": []}'
    assert seen['path'] == '/v1/messages'
    assert seen['headers']['x-api-key'] == API_KEY
    assert seen['headers']['anthropic-version'] == '2023-06-01'
    assert 'authorization' not in {k.lower() for k in seen['headers']}
    assert seen['body']['system'] == 'rules'
    assert seen['body']['messages'] == [{'role': 'user', 'content': 'hi'}]
    assert seen['body']['max_tokens'] == 64
    assert seen['body']['output_config'] == {'effort': 'low'}


@pytest.mark.parametrize('effort,expected', [(None, None), ('none', None), ('low', 'low')])
def test_anthropic_omits_none_effort(effort, expected):
    """Anthropic is the only provider that drops an explicit ``none``."""
    seen, handler = recorder({'content': [{'type': 'text', 'text': 'ok'}]})
    provider = build(AnthropicProvider, handler, base_url='https://api.anthropic.com')
    provider.complete([{'role': 'user', 'content': 'hi'}], model='claude-x', effort=effort,
                       json_schema=None, max_output_tokens=64)
    if expected is None:
        assert 'output_config' not in seen['body']
    else:
        assert seen['body']['output_config'] == {'effort': expected}


def test_anthropic_extra_body_overrides_output_config():
    seen, handler = recorder({'content': [{'type': 'text', 'text': 'ok'}]})
    provider = build(AnthropicProvider, handler, base_url='https://api.anthropic.com',
                     extra_body={'output_config': {'effort': 'custom'}})
    provider.complete([{'role': 'user', 'content': 'hi'}], model='claude-x', effort='low',
                       json_schema=None, max_output_tokens=64)
    assert seen['body']['output_config'] == {'effort': 'custom'}


def test_anthropic_base_url_already_ending_in_v1():
    seen, handler = recorder({'content': [{'type': 'text', 'text': 'ok'}]})
    provider = build(AnthropicProvider, handler, base_url='https://api.anthropic.com/v1')
    provider.complete([{'role': 'user', 'content': 'hi'}], model='claude-x', effort=None,
                       json_schema=None, max_output_tokens=64)
    assert seen['path'] == '/v1/messages'


def test_anthropic_model_list_follows_pagination():
    pages = [{'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'},
             {'data': [{'id': 'b'}], 'has_more': False}]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        page = pages[min(len(calls) - 1, len(pages) - 1)]
        return httpx.Response(200, json=page)

    provider = build(AnthropicProvider, handler, base_url='https://api.anthropic.com')
    models = provider.list_models()
    assert [m.id for m in models] == ['a', 'b']
    assert 'after_id=a' in calls[1]


# ---------------------------------------------------------------- minimax ----
def test_minimax_modern_model_uses_chat_completions_with_thinking():
    seen, handler = recorder({'base_resp': {'status_code': 0}, **ANSWER})
    provider = build(MiniMaxProvider, handler, base_url='https://api.minimaxi.com/v1')
    provider.complete([{'role': 'user', 'content': 'hi'}], model='MiniMax-M3.1-Flash', effort=None,
                       json_schema=None, max_output_tokens=8192)
    assert seen['path'] == '/v1/chat/completions'
    assert seen['body']['thinking'] == {'type': 'adaptive'}
    assert seen['body']['max_completion_tokens'] == 8192
    assert seen['body']['reasoning_effort'] == 'low'


def test_minimax_legacy_model_uses_text_chatcompletion_v2():
    seen, handler = recorder({'base_resp': {'status_code': 0}, **ANSWER})
    provider = build(MiniMaxProvider, handler, base_url='https://api.minimaxi.com')
    provider.complete([{'role': 'user', 'content': 'hi'}], model='MiniMax-M2.7', effort=None,
                       json_schema=None, max_output_tokens=8192)
    assert seen['path'] == '/v1/text/chatcompletion_v2'
    assert 'thinking' not in seen['body']
    assert 'reasoning_effort' not in seen['body']


@pytest.mark.parametrize('code,retryable', [(1004, False), (1008, False), (2049, False),
                                             (2056, False), (1002, True)])
def test_minimax_business_status_codes(code, retryable):
    _, handler = recorder({'base_resp': {'status_code': code}, **ANSWER})
    provider = build(MiniMaxProvider, handler, base_url='https://api.minimaxi.com/v1')
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='MiniMax-M3', effort=None,
                           json_schema=None, max_output_tokens=8192)
    assert error.value.retryable is retryable
    assert error.value.business_code == code


@pytest.mark.parametrize('code', ['1002', 1002.5, True, {'code': 1004}])
def test_minimax_malformed_business_status_is_not_silently_accepted(code):
    _, handler = recorder({'base_resp': {'status_code': code}, **ANSWER})
    provider = build(MiniMaxProvider, handler, base_url='https://api.minimaxi.com/v1')
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='MiniMax-M3', effort=None,
                           json_schema=None, max_output_tokens=8192)
    assert error.value.retryable is True
    assert error.value.error_type == 'InvalidPayload'


def test_minimax_model_list_falls_back_to_the_builtin_catalogue():
    def handler(request):
        return httpx.Response(500, json={'error': 'nope'})

    provider = build(MiniMaxProvider, handler, base_url='https://api.minimaxi.com/v1')
    models = provider.list_models()
    assert any(m.id == 'MiniMax-M3.1-Flash' for m in models)


# ----------------------------------------------------------------- errors ----
@pytest.mark.parametrize('status,retryable', [(401, False), (403, False), (400, False),
                                              (408, True), (429, True), (500, True)])
def test_http_status_mapping(status, retryable):
    _, handler = recorder({'error': 'x'}, status=status)
    provider = build(ChatCompletionsProvider, handler)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort=None,
                           json_schema=None, max_output_tokens=10)
    assert error.value.retryable is retryable
    assert error.value.status_code == status


def test_transport_failure_is_retryable_and_hides_the_key():
    def handler(request):
        raise httpx.ConnectError('offline', request=request)

    provider = build(ChatCompletionsProvider, handler)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-x', effort=None,
                           json_schema=None, max_output_tokens=10)
    assert error.value.retryable is True
    assert API_KEY not in str(error.value)


def test_missing_key_is_permanent():
    with pytest.raises(ProviderError) as error:
        ChatCompletionsProvider(api_key='', base_url='https://api.example.com/v1')
    assert error.value.retryable is False


# ------------------------------------------------------------ api style ----
@pytest.mark.parametrize('base_url,expected', [
    ('https://api.anthropic.com/v1', 'anthropic'),
    ('https://api.minimaxi.com/v1', 'chat-completions'),
    ('https://api.openai.com/v1', 'openai-compatible'),
    ('https://proxy.internal.example.com/v1', 'chat-completions'),
    (None, 'chat-completions'),
])
def test_detect_api_style(base_url, expected):
    assert detect_api_style(base_url) == expected


def test_effort_helpers():
    assert normalize_effort(None) is None
    assert normalize_effort('none') == 'none'
    assert omit_none_effort('none') is None
    assert omit_none_effort('low') == 'low'
