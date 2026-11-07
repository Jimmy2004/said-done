"""In-process SDK calls; no transport, keychain, network, or real CLIs."""
import asyncio
import importlib
import json
import sys
import types

import pytest
from pydantic import SecretStr

from said_done.config import Settings, save_settings
from said_done.mcp_server import services
from said_done.mcp_server.server import TOOL_NAMES, create_server

KEY = 'sk-test-never-return-this-credential-123456'


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv('SAID_DONE_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('SAID_DONE_HOME', str(tmp_path / 'home'))
    settings = Settings(data_dir=tmp_path / 'data', screening_only=True)
    save_settings(settings, services.config_path())
    try:
        credentials = importlib.import_module('said_done.credentials')
    except ImportError:
        credentials = types.ModuleType('said_done.credentials')
        monkeypatch.setitem(sys.modules, 'said_done.credentials', credentials)
    monkeypatch.setattr(credentials, 'get_api_key', lambda *a: SecretStr(KEY), raising=False)
    monkeypatch.setattr(credentials, 'credential_status', lambda *a: {'exists': True}, raising=False)
    return create_server(), settings


def call(server, name, **arguments):
    response = asyncio.run(server.call_tool(name, arguments))
    value = response.model_dump(mode='json')
    assert KEY not in json.dumps(value)
    # SDK 1.x and 2.x both provide JSON text content.
    return json.loads(next(block.text for block in response.content if block.type == 'text'))


def test_registration_schema_resource_prompt(server):
    mcp, _ = server
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == set(TOOL_NAMES)
    for tool in tools:
        assert 'settings' not in tool.input_schema.get('properties', {})
        assert 'path' not in tool.input_schema.get('properties', {})
    audit = next(t for t in tools if t.name == 'audit_records')
    assert audit.input_schema['properties']['include_evidence']['default'] is False
    assert str(asyncio.run(mcp.list_resources())[0].uri) == 'guide://setup'
    assert asyncio.run(mcp.list_prompts())[0].name == 'setup'
    assert 'Always communicate' in call(mcp, 'get_setup_guide')['guide']
    assert 'Always communicate' in asyncio.run(mcp.read_resource('guide://setup'))[0].content
    assert 'Always communicate' in asyncio.run(mcp.get_prompt('setup', {})).messages[0].content.text


def test_all_tool_outputs_scrub_known_key(server, monkeypatch):
    mcp, _ = server
    # Each registration delegates lazily to its service and applies the same boundary.
    samples = {'configure_classifier': {'patch': {}}, 'configure_screening': {'patch': {}},
               'configure_executor': {'patch': {}}, 'configure_sources': {'sources': []},
               'submit_transcript': {'text': 'synthetic'}, 'task_result': {'task_id': 't'},
               'cancel_task': {'task_id': 't'}, 'complete_task': {'task_id': 't', 'summary': 'done'},
               'release_task': {'task_id': 't'}}
    for name in TOOL_NAMES:
        monkeypatch.setattr(services, name, lambda **kw: {'result': [KEY, {'echo': KEY}]})
        result = call(mcp, name, **samples.get(name, {}))
        assert result['result'] == ['[REDACTED]', {'echo': '[REDACTED]'}]


def test_errors_do_not_echo_transcript_or_credentials(server, monkeypatch):
    mcp, _ = server
    def fail(**kw):
        raise ValueError(KEY + ' private transcript')
    monkeypatch.setattr(services, 'submit_transcript', fail)
    result = call(mcp, 'submit_transcript', text='private transcript')
    assert result['error'] == 'ValueError'
    assert 'private transcript' not in json.dumps(result)
    assert KEY not in json.dumps(result)


def test_errors_are_actionable_without_echoing_input(server, monkeypatch):
    mcp, _ = server
    def fail(**kw):
        raise ValueError('select a configured manual source')
    monkeypatch.setattr(services, 'submit_transcript', fail)
    result = call(mcp, 'submit_transcript', text='hello there')
    assert result == {'error': 'ValueError', 'message': 'select a configured manual source'}


def test_fresh_setup_starts_screening_only(server):
    mcp, settings = server
    services.config_path().unlink()
    result = call(mcp, 'setup_status')
    assert result['screening_only'] is True


def test_end_to_end_setup_submit_execute_result(server, monkeypatch):
    mcp, settings = server
    # A/B contracts are mocked only when unavailable; runtime, storage, screening,
    # execution and artifact validation in this flow remain real local mock code.
    try:
        importlib.import_module('said_done.providers')
    except ImportError:
        providers = types.ModuleType('said_done.providers')
        providers.SUBSCRIPTION_DISCLAIMER = 'subscription risk'
        monkeypatch.setitem(sys.modules, 'said_done.providers', providers)
    try:
        importlib.import_module('said_done.ingestion.registry')
    except ImportError:
        registry = types.ModuleType('said_done.ingestion.registry')
        registry.list_source_kinds = lambda: [{'kind': 'manual'}]
        monkeypatch.setitem(sys.modules, 'said_done.ingestion.registry', registry)
    try:
        importlib.import_module('said_done.executor.registry')
    except ImportError:
        registry = types.ModuleType('said_done.executor.registry')
        registry.executor_info = lambda kind: {'kind': kind, 'verification': 'verified', 'sandbox': 'mock'}
        monkeypatch.setitem(sys.modules, 'said_done.executor.registry', registry)
    assert call(mcp, 'configure_classifier', patch={'kind': 'mock'})['configured']
    assert call(mcp, 'test_classifier')['ok']
    assert call(mcp, 'configure_sources', sources=[{'id': 'manual', 'kind': 'manual'}])['sources']
    assert call(mcp, 'configure_executor', patch={'kind': 'mock'})['executor']['kind'] == 'mock'
    assert call(mcp, 'test_executor')['preflight']['status'] == 'READY'
    assert call(mcp, 'configure_screening', patch={'screening_only': False})['values']['screening_only'] is False
    assert call(mcp, 'submit_transcript', text='请帮我写一个Python脚本，输出hello world。', segment_id='e2e')['inserted'] == 1
    processed = call(mcp, 'process_now')
    assert not processed.get('error'), processed
    assert not processed['busy']
    tasks = call(mcp, 'status')['tasks']
    assert len(tasks) == 1
    result = call(mcp, 'task_result', task_id=tasks[0]['id'])['result']
    assert result['status'] == 'COMPLETED'
    assert result['artifacts'] and all(__import__('pathlib').Path(p).is_file() for p in result['artifacts'])


@pytest.mark.parametrize('name,args', [
    ('configure_classifier', {'patch': KEY}),
    ('list_tasks', {'limit': KEY}),
    ('submit_transcript', {'text': {'api_key': KEY}}),
])
def test_sdk_validation_does_not_echo_inputs(server, caplog, name, args):
    mcp, _ = server
    result = call(mcp, name, **args)
    assert result['error'] == 'InvalidArguments'
    assert KEY not in caplog.text
