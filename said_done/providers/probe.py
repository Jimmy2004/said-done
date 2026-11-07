"""Discover wire protocols using model-free validation requests only."""
from __future__ import annotations

import re

import httpx

_TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')


def probe_api(base_url, api_key, *, client=None, timeout=10) -> dict:
    base = (base_url or '').rstrip('/')
    key = api_key.get_secret_value() if hasattr(api_key, 'get_secret_value') else api_key
    bearer = {'Authorization': f'Bearer {key}'}
    anthropic = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    requests = [
        ('GET', '/models', bearer, None),
        ('GET', '/models', anthropic, None),
        ('POST', '/chat/completions', bearer, {'messages': [{'role': 'user', 'content': 'ping'}]}),
        ('POST', '/responses', bearer, {'input': 'ping'}),
        ('POST', '/messages', anthropic, {'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'ping'}]}),
        ('POST', '/v1/messages', anthropic, {'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'ping'}]}),
    ]
    owned = client is None
    client = client if client is not None else httpx.Client()
    probes = []
    try:
        for method, suffix, headers, body in requests:
            status, result, token = None, 'unknown', None
            try:
                response = client.request(method, base + suffix, headers=headers,
                                          **({'json': body} if body is not None else {}),
                                          timeout=timeout, follow_redirects=False)
                status = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                error = payload.get('error') if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    for field in ('type', 'code'):
                        candidate = error.get(field)
                        if isinstance(candidate, str) and _TOKEN.fullmatch(candidate) and not (key and key in candidate):
                            token = candidate
                            break
                if 200 <= status < 300 or (status in (400, 422) and isinstance(payload, dict) and 'error' in payload):
                    result = 'available'
                elif status in (401, 403):
                    result = 'auth_failed'
                elif status in (404, 405):
                    result = 'absent'
            except Exception as exc:
                candidate = type(exc).__name__
                token = candidate if _TOKEN.fullmatch(candidate) and not (key and key in candidate) else None
            scheme = 'x-api-key' if 'x-api-key' in headers else 'bearer'
            probes.append({'endpoint': f'{method} {suffix}', 'auth_scheme': scheme, 'status': status,
                           'result': result, 'error_type': token})
    finally:
        if owned:
            client.close()
    available = [p['result'] == 'available' for p in probes]
    styles = [style for style, works in zip(
        ('chat-completions', 'openai-compatible', 'anthropic'),
        (available[2], available[3], any(available[4:]))) if works]
    anthropic_base = base if available[5] else (base[:-3] if available[4] and base.endswith('/v1') else None)
    if key and anthropic_base and key in anthropic_base:
        anthropic_base = None
    detected = styles[0] if styles else None
    auth = ('bearer' if detected in ('chat-completions', 'openai-compatible') else
            'x-api-key' if detected == 'anthropic' else
            'bearer' if available[0] else 'x-api-key' if available[1] else None)
    result = {'supported_styles': styles, 'detected': detected, 'auth': auth,
              'models_endpoint': any(available[:2]), 'anthropic_base_url': anthropic_base,
              'probes': probes}
    if 'anthropic' in styles and anthropic_base is None:
        result['warnings'] = ['Anthropic endpoint found, but base_url cannot be normalized to /v1/messages; check base_url.']
    return result
