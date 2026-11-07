import json

import httpx
import pytest

from said_done.providers.probe import probe_api

KEY = 'sk-private-probe-key'


@pytest.fixture
def minimax_transport():
    def handle(request):
        path = request.url.path
        if request.method == 'POST':
            assert 'model' not in json.loads(request.content)
        if path.startswith('/anthropic'):
            return httpx.Response(400, json={'type': 'error', 'error': {'type': 'invalid_request_error'}}) if path == '/anthropic/v1/messages' else httpx.Response(404)
        if path == '/v1/models':
            if 'x-api-key' in request.headers:
                return httpx.Response(401, json={'error': {'type': 'authorized_error'}})
            return httpx.Response(200, json={'object': 'list', 'data': []})
        if path == '/v1/chat/completions':
            bad = request.headers['authorization'] != 'Bearer ' + KEY
            return httpx.Response(401 if bad else 400, json={'error': {'type': 'authorized_error' if bad else 'bad_request_error'}})
        if path == '/v1/responses':
            return httpx.Response(400, json={'error': {'code': 'invalid_prompt'}})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


@pytest.mark.parametrize('base,styles,auth', [
    ('https://api.minimaxi.com/v1', ['chat-completions', 'openai-compatible'], 'bearer'),
    ('https://api.minimaxi.com/anthropic', ['anthropic'], 'x-api-key'),
])
def test_minimax(minimax_transport, base, styles, auth):
    with httpx.Client(transport=minimax_transport) as client:
        result = probe_api(base, KEY, client=client)
    assert result['supported_styles'] == styles
    assert result['detected'] == styles[0] and result['auth'] == auth
    assert result['anthropic_base_url'] == (base if styles == ['anthropic'] else None)
    assert result['models_endpoint'] == (auth == 'bearer')
    assert len(result['probes']) == 6 and KEY not in json.dumps(result)


@pytest.mark.parametrize('status,body,expected', [
    (200, {}, 'available'), (204, None, 'available'),
    (400, {'error': {'type': 'invalid_request_error'}}, 'available'),
    (422, {'error': {'code': 'invalid_prompt'}}, 'available'),
    (400, {}, 'unknown'), (422, None, 'unknown'),
    (401, {}, 'auth_failed'), (403, {}, 'auth_failed'),
    (404, None, 'absent'), (405, None, 'absent'), (500, {}, 'unknown'), (503, {}, 'unknown'),
])
def test_matrix(status, body, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))) as client:
        result = probe_api('https://neutral.test', KEY, client=client)
    assert all(p['result'] == expected and p['status'] == status for p in result['probes'])
    if expected != 'available':
        assert result['supported_styles'] == [] and result['detected'] is None


@pytest.mark.parametrize('exc', [httpx.ConnectError, httpx.ReadTimeout])
def test_network(exc):
    def handle(request):
        raise exc(KEY)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe_api('https://neutral.test', KEY, client=client)
    assert all(p['status'] is None and p['result'] == 'unknown' and p['error_type'] == exc.__name__ for p in result['probes'])
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize('token', [KEY, 'unsafe error message', 'x'*65, 'valid.code:123'])
def test_error_scrubbing(token):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={'error': {'type': token, 'message': KEY}}))) as client:
        result = probe_api('https://neutral.test', KEY, client=client)
    assert KEY not in json.dumps(result)
    assert result['probes'][0]['error_type'] == ('valid.code:123' if token == 'valid.code:123' else None)


@pytest.mark.parametrize('base,path,normalized', [
    ('https://neutral.test', '/v1/messages', 'https://neutral.test'),
    ('https://neutral.test/v1', '/v1/messages', 'https://neutral.test'),
    ('https://neutral.test/custom', '/custom/messages', None),
])
def test_anthropic(base, path, normalized):
    def handle(r):
        available = r.url.path == path or (r.method == 'GET' and 'x-api-key' in r.headers)
        return httpx.Response(422, json={'error': {'type': 'invalid_request_error'}}) if available else httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe_api(base, KEY, client=client)
    assert result['detected'] == 'anthropic' and result['auth'] == 'x-api-key'
    assert result['models_endpoint'] and result['anthropic_base_url'] == normalized
    assert bool(result.get('warnings')) == (normalized is None)


def test_mcp_probe(monkeypatch):
    from pydantic import SecretStr
    from said_done.config import Settings
    from said_done.mcp_server import services
    from said_done.mcp_server.server import TOOL_NAMES
    monkeypatch.setattr('said_done.credentials.get_api_key', lambda settings, profile: SecretStr(KEY))
    def fake(base, key):
        assert base == 'https://neutral.test' and key == KEY
        return {'detected': 'anthropic', 'unsafe': KEY}
    monkeypatch.setattr('said_done.providers.probe.probe_api', fake)
    result = services.probe_api(Settings(classifier={'base_url': 'https://neutral.test'}), 'other')
    assert 'probe_api' in TOOL_NAMES
    assert result['detected'] == 'anthropic' and KEY not in json.dumps(result)
