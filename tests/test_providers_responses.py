"""Offline Responses and API style regression coverage."""
import json

import httpx
import pytest

from said_done.providers import OpenAICompatibleProvider, ChatCompletionsProvider, detect_api_style, ProviderError
from said_done.credentials.parsing import detect_api_style as credential_style
from said_done.credentials.template import TEMPLATE_TEXT


def provider(payload, **kwargs):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=payload)
    return OpenAICompatibleProvider(api_key='fake-key', client=httpx.Client(
        transport=httpx.MockTransport(handle)), **kwargs), seen


def complete(p, effort=None):
    return p.complete([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'hi'}],
                      model='m', effort=effort, json_schema=None, max_output_tokens=42)


@pytest.mark.parametrize('effort', [None, 'none', 'low', 'high'])
def test_responses_request(effort):
    p, seen = provider({'output_text': 'ok'})
    assert complete(p, effort) == 'ok'
    request = seen[0]
    assert request.url.path == '/v1/responses'
    assert request.method == 'POST'
    assert request.headers['authorization'] == 'Bearer fake-key'
    body = json.loads(request.content)
    assert body['model'] == 'm' and body['max_output_tokens'] == 42
    assert body['input'][0] == {'role': 'system', 'content': 'rules'}
    assert body.get('reasoning') == ({'effort': effort} if effort not in (None, 'none') else None)
    assert 'reasoning_effort' not in body


def test_default_effort_and_extra_body():
    p, seen = provider({'output_text': 'ok'}, effort='high', extra_body={'max_output_tokens': 7})
    complete(p)
    assert json.loads(seen[0].content)['reasoning'] == {'effort': 'high'}
    assert json.loads(seen[0].content)['max_output_tokens'] == 7


def test_output_parts():
    p, _ = provider({'output': [{'type': 'reasoning', 'content': []},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'one'},
                                      {'type': 'refusal', 'text': 'ignored'},
                                      {'type': 'output_text', 'text': 'two'}]},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'three'}]}]})
    assert complete(p) == 'onetwothree'


@pytest.mark.parametrize('payload,category', [
    ({'status': 'incomplete', 'output_text': 'partial'}, 'IncompleteResponse'),
    ({'error': {'type': 'server_error', 'message': 'private body'}}, 'server_error'),
    ({'error': {'code': 'bad_code', 'message': 'private body'}}, 'bad_code'),
    ({'error': {}, 'output_text': 'ok'}, 'ProviderError'),
    ({'output': []}, 'EmptyContent'),
])
def test_errors(payload, category):
    p, _ = provider(payload)
    with pytest.raises(ProviderError) as exc:
        complete(p)
    assert exc.value.error_type == category
    assert 'private body' not in str(exc.value)


def test_models():
    p, seen = provider({'data': [{'id': 'm'}, {'id': 'n'}]})
    assert [m.id for m in p.list_models()] == ['m', 'n']
    assert seen[0].method == 'GET' and seen[0].url.path == '/v1/models'


@pytest.mark.parametrize('url,style', [
    ('https://api.openai.com/v1', 'openai-compatible'),
    ('https://api.anthropic.com', 'anthropic'),
    ('https://api.openai.com.evil.test/v1', 'chat-completions'),
    ('https://gateway.test/anthropic.com', 'chat-completions'),
    ('https://api.minimaxi.com/v1', 'chat-completions'),
    ('https://api.deepseek.com', 'chat-completions'),
    (None, 'chat-completions'),
])
def test_detection_matrix(url, style):
    assert detect_api_style(url) == credential_style(url) == style


def test_template():
    assert 'openai-compatible（OpenAI Responses API）| anthropic | chat-completions（/chat/completions：MiniMax、DeepSeek、OpenRouter、各类网关等）' in TEMPLATE_TEXT
    assert '留空则自动探测' in TEMPLATE_TEXT
    assert '不消耗 token' in TEMPLATE_TEXT


def test_chat_models_fallback(caplog):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    p = ChatCompletionsProvider(api_key='fake', client=client)
    assert p.list_models() == []
    assert 'user must supply the model ID' in caplog.text
