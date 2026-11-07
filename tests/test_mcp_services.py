"""Offline contract and security coverage for the MCP service layer."""
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from said_done.config import Settings, load_settings, save_settings
from said_done.mcp_server import services

KEY = 'sk-test-never-return-this-credential-123456'


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv('SAID_DONE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SAID_DONE_CONFIG', str(tmp_path / 'config.json'))
    return Settings(data_dir=tmp_path / 'data', screening_only=True)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    try:
        module = importlib.import_module('said_done.credentials')
    except ImportError:
        module = types.ModuleType('said_done.credentials')
        monkeypatch.setitem(sys.modules, 'said_done.credentials', module)
    monkeypatch.setattr(module, 'get_api_key', lambda *a, **k: SecretStr(KEY), raising=False)
    monkeypatch.setattr(module, 'credential_status', lambda *a, **k: {'exists': True, 'backend': 'memory'}, raising=False)
    return module


def stub(monkeypatch, name, **functions):
    module = types.ModuleType(name)
    module.__dict__.update(functions)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_import_persists_patch_without_internal_fields(settings, credentials, monkeypatch):
    monkeypatch.setattr(credentials, 'import_credentials', lambda *a: {
        'stored': True, 'key_fingerprint': 'sha256:12345678', 'key_length': len(KEY),
        'updated_classifier': {'kind': 'anthropic', 'base_url': 'https://api.example.test'}}, raising=False)
    result = services.import_credentials(settings)
    assert 'updated_classifier' not in result
    saved = load_settings(services.config_path())
    assert saved.classifier.kind == 'anthropic'
    assert saved.classifier.profile == 'default'
    assert KEY not in json.dumps(result)
    assert services.config_path().stat().st_mode & 0o777 == 0o600


def test_setup_begin_enables_screening(settings, credentials, monkeypatch):
    monkeypatch.setattr(credentials, 'begin_import', lambda *a: {'path': '/tmp/credentials-default.txt'}, raising=False)
    unsafe = settings.model_copy(deep=True)
    unsafe.screening.screening_only = False
    assert services.setup_begin(unsafe)['path'].endswith('.txt')
    assert load_settings(services.config_path()).screening.screening_only


@pytest.mark.parametrize('kind', ['codex-subscription', 'claude-subscription'])
def test_subscription_requires_explicit_acknowledgement(settings, monkeypatch, kind):
    stub(monkeypatch, 'said_done.providers', SUBSCRIPTION_DISCLAIMER='subscription risk disclaimer')
    rejected = services.configure_classifier(settings, {'kind': kind})
    assert rejected['requires_acknowledgement'] and not rejected['configured']
    assert not services.config_path().exists()
    accepted = services.configure_classifier(settings, {'kind': kind, 'acknowledge_subscription_risk': True})
    assert accepted['configured'] and accepted['disclaimer'] == rejected['disclaimer']
    # Reconfiguration is also an explicit acknowledgement, not inherited consent.
    saved = load_settings(services.config_path())
    assert not services.configure_classifier(saved, {'model': 'other'})['configured']


def test_screening_merge_and_reset(settings):
    changed = services.configure_screening(settings, {'batch': {'min_content_chars': 42}})
    assert changed['values']['batch']['min_content_chars'] == 42
    assert changed['values']['batch']['max_batch_segments'] == 50
    assert changed['differences'] == {'batch': {'min_content_chars': 42}}
    reset = services.reset_screening_defaults(load_settings(services.config_path()))
    assert reset['differences'] == {}
    assert reset['values']['screening_only'] is True


@pytest.mark.parametrize('field', ['api_key', 'authorization', 'access_token'])
def test_reject_inline_credentials(settings, field):
    with pytest.raises(ValueError, match='credential importer'):
        services.configure_screening(settings, {field: KEY})
    assert not services.config_path().exists()


def test_executor_returns_all_warnings(settings, monkeypatch):
    info = {'kind': 'pi', 'verification': 'experimental-unverified',
            'sandbox': 'none', 'notes': ['No OS sandbox; verify flags locally.']}
    stub(monkeypatch, 'said_done.executor.registry', executor_info=lambda kind: info)
    result = services.configure_executor(settings, {'kind': 'pi'})
    assert result['needs_user_verification']
    assert result['sandbox'] == 'none'
    assert result['warnings'] == info['notes']


def test_executor_is_preflight_only(settings, monkeypatch):
    stub(monkeypatch, 'said_done.executor.registry', executor_info=lambda kind: {'kind': kind})
    result = services.test_executor(settings)
    assert result['dry_run']
    assert result['preflight']['status'] == 'READY'


@pytest.mark.parametrize('error', [BlockingIOError(), RuntimeError('another worker is active')])
def test_lock_contention_is_busy_and_closes(settings, monkeypatch, error):
    closed = []
    def tick(**kwargs):
        raise error
    runtime = SimpleNamespace(tick=tick, close=lambda: closed.append(True))
    monkeypatch.setattr('said_done.runtime.create_runtime', lambda s: runtime)
    assert services.process_now(settings) == {'busy': True}
    assert closed == [True]


def test_other_runtime_error_propagates(settings, monkeypatch):
    def create(s):
        raise RuntimeError('different error')
    monkeypatch.setattr('said_done.runtime.create_runtime', create)
    with pytest.raises(RuntimeError, match='different error'):
        services.process_now(settings)


def test_audit_evidence_opt_in(settings, monkeypatch):
    record = {'id': 'one', 'action': 'IGNORE', 'reason': 'not actionable',
              'text': 'private transcript', 'original_evidence': ['private quote'],
              'evidence_segment_ids': ['s1'], 'source_refs': [{'source': 'manual'}]}
    monkeypatch.setattr(services, '_repo', lambda s: SimpleNamespace(audit_records=lambda **kw: [record]))
    default = services.audit_records(settings)['records'][0]
    assert default == {'id': 'one', 'action': 'IGNORE', 'reason': 'not actionable'}
    assert services.audit_records(settings, include_evidence=True)['records'][0] == record


def test_status_is_observable_and_does_not_claim_tests(settings):
    fresh = services.setup_status(settings)
    assert fresh['next_step'] == 'classifier'
    assert not any(s['complete'] for s in fresh['steps'])
    save_settings(settings, services.config_path())
    saved = services.setup_status(settings)
    assert all(s['complete'] for s in saved['steps'])
    assert saved['next_step'] == 'review_screening'
    assert not saved['tests_verified'] and not saved['daemon']['verified']


def test_source_configuration(settings, monkeypatch):
    stub(monkeypatch, 'said_done.ingestion.registry', list_source_kinds=lambda: [{'kind': 'manual'}])
    result = services.configure_sources(settings, [{'id': 'notes', 'kind': 'manual', 'options': {}}])
    assert result['sources'][0]['id'] == 'notes'
    with pytest.raises(ValueError, match='not installed'):
        services.configure_sources(settings, [{'id': 'bad', 'kind': 'missing'}])


def test_submit_validates_source_time_and_does_not_return_text(settings):
    result = services.submit_transcript(settings, 'private transcript', segment_id='s1',
        started_at='2026-11-07T00:00:00Z', ended_at='2026-11-07T00:00:01Z')
    assert result == {'inserted': 1, 'segment_id': 's1', 'source': 'manual'}
    assert services.submit_transcript(settings, 'private transcript', segment_id='s1',
        started_at='2026-11-07T00:00:00Z', ended_at='2026-11-07T00:00:01Z')['inserted'] == 0
    with pytest.raises(ValueError):
        services.submit_transcript(settings, 'text', source='not-configured')
    with pytest.raises(ValueError):
        services.submit_transcript(settings, 'text', started_at='not-a-time')


def test_host_contract_always_validates(settings, monkeypatch):
    calls = []
    stub(monkeypatch, 'said_done.broker.host_queue',
         claim_next_task=lambda repo, lease_seconds: {'task_id': 't', 'lease_seconds': lease_seconds},
         complete_task=lambda repo, task, summary, validate: calls.append(validate) or {'complete': True},
         release_task=lambda repo, task: {'released': True})
    assert services.claim_next_task(settings)['task']['lease_seconds'] == 7200
    assert services.complete_task(settings, 't', 'done')['complete']
    assert calls == [True]
    assert services.release_task(settings, 't')['released']


def test_public_result_redacts_recursive_values_and_keys(settings):
    result = services.public_result(settings, {'message': KEY, 'nested': [KEY], KEY: 'key name'})
    assert KEY not in json.dumps(result)
    assert result['nested'] == ['[REDACTED]']
