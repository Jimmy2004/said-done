"""Screening model defaults: the cheapest model per provider kind (docs/DESIGN.md §11).

Screening is a light classification, so a subscription falls back to the
cheapest model its CLI serves.  An API provider has no built-in default: an
unset model is a configuration error the setup agent resolves through
``list_models``.

No network, no keyring and no real CLI: the subscription CLIs are fakes and
the HTTP catalogues are served by ``httpx.MockTransport``.
"""
import json
import os
import stat
import sys

import httpx
import pytest

from said_done.config import Settings
from said_done.providers import (ClaudeSubscriptionProvider, CodexSubscriptionProvider,
                           OpenAICompatibleProvider, default_model_for_kind, list_models)
from said_done.providers.claude_subscription import DEFAULT_SCREENING_MODEL as CLAUDE_DEFAULT
from said_done.providers.codex_subscription import DEFAULT_SCREENING_MODEL as CODEX_DEFAULT
from said_done.semantic.factory import build_classifier, resolve_screening_model

API_KEY = 'test-only-key'

CODEX_FAKE = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
record = os.environ.get("SAID_DONE_TEST_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(args, handle)
sys.stdin.read()
answer = json.dumps({"decisions": []})
with open(args[args.index("-o") + 1], "w", encoding="utf-8") as handle:
    handle.write(answer)
sys.stdout.write(answer)
'''

CLAUDE_FAKE = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
record = os.environ.get("SAID_DONE_TEST_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(args, handle)
sys.stdin.read()
sys.stdout.write(json.dumps({"result": json.dumps({"decisions": []}), "is_error": False}))
'''


def make_cli(tmp_path, source, name='fake-cli'):
    path = tmp_path / name
    path.write_text(source.replace('#!/usr/bin/env python3', f'#!{sys.executable}'), encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def settings_for(kind='mock', **classifier):
    payload = {'kind': kind}
    payload.update(classifier)
    return Settings(classifier=payload)


class Repository:
    def semantic_context(self, batch=None):
        return []


@pytest.fixture(autouse=True)
def no_stored_credentials(monkeypatch):
    monkeypatch.delenv('SAID_DONE_API_KEY_DEFAULT', raising=False)
    monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
    import said_done.credentials
    monkeypatch.setattr(said_done.credentials, 'get_api_key', lambda *args, **kwargs: None)


# ------------------------------------------------------------- catalogues ---
def test_the_codex_catalogue_leads_with_the_cheapest_model():
    models = CodexSubscriptionProvider(acknowledge_subscription_risk=True).list_models()
    assert models[0].id == CODEX_DEFAULT == 'gpt-6-luna'
    assert models[0].recommended_for_screening is True
    assert '最便宜' in (models[0].display_name or '')
    assert 'default' in {model.id for model in models}


def test_the_stale_codex_models_are_gone():
    ids = {model.id for model in
           CodexSubscriptionProvider(acknowledge_subscription_risk=True).list_models()}
    assert 'gpt-5-codex' not in ids
    assert 'gpt-5' not in ids


def test_the_claude_catalogue_leads_with_the_cheapest_model():
    models = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True).list_models()
    assert models[0].id == CLAUDE_DEFAULT == 'claude-haiku-5-5'
    assert models[0].recommended_for_screening is True
    assert '最便宜' in (models[0].display_name or '')
    assert {'default', 'opus', 'sonnet', 'haiku'} <= {model.id for model in models}


def test_only_one_model_per_catalogue_is_recommended():
    for provider in (CodexSubscriptionProvider(acknowledge_subscription_risk=True),
                     ClaudeSubscriptionProvider(acknowledge_subscription_risk=True)):
        recommended = [model.id for model in provider.list_models()
                       if model.recommended_for_screening]
        assert len(recommended) == 1


def test_the_default_model_per_kind_is_exposed():
    assert default_model_for_kind('codex-subscription') == 'gpt-6-luna'
    assert default_model_for_kind('claude-subscription') == 'claude-haiku-5-5'
    assert default_model_for_kind('openai-compatible') is None
    assert default_model_for_kind('anthropic') is None
    assert default_model_for_kind('minimax') is None


def test_list_models_carries_the_screening_recommendation():
    models = list_models(settings_for('codex-subscription', acknowledge_subscription_risk=True))
    assert models[0]['id'] == 'gpt-6-luna'
    assert models[0]['recommended_for_screening'] is True
    claude = list_models(settings_for('claude-subscription', acknowledge_subscription_risk=True))
    assert claude[0]['id'] == 'claude-haiku-5-5'
    assert claude[0]['recommended_for_screening'] is True


# ------------------------------------------------------- provider default ---
def test_a_subscription_provider_defaults_to_its_cheapest_model():
    assert CodexSubscriptionProvider(acknowledge_subscription_risk=True).model == 'gpt-6-luna'
    assert ClaudeSubscriptionProvider(acknowledge_subscription_risk=True).model == 'claude-haiku-5-5'


def test_an_explicit_model_still_wins():
    assert CodexSubscriptionProvider(acknowledge_subscription_risk=True,
                                     model='chosen-custom-model').model == 'chosen-custom-model'
    assert ClaudeSubscriptionProvider(acknowledge_subscription_risk=True,
                                      model='sonnet').model == 'sonnet'


def test_the_codex_cli_receives_the_default_model(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = tmp_path / 'argv.json'
    monkeypatch.setenv('SAID_DONE_TEST_RECORD', str(record))
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model=provider.model, effort=None,
                      json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert argv[argv.index('-m') + 1] == 'gpt-6-luna'


def test_the_claude_cli_receives_the_default_model(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CLAUDE_FAKE, 'claude')
    record = tmp_path / 'argv.json'
    monkeypatch.setenv('SAID_DONE_TEST_RECORD', str(record))
    provider = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model=provider.model, effort=None,
                      json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert argv[argv.index('--model') + 1] == 'claude-haiku-5-5'


def test_the_default_alias_still_means_the_cli_default(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = tmp_path / 'argv.json'
    monkeypatch.setenv('SAID_DONE_TEST_RECORD', str(record))
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                      json_schema=None, max_output_tokens=100)
    assert '-m' not in json.loads(record.read_text())


# ------------------------------------------------------- semantic factory ---
def test_the_classifier_falls_back_to_the_cheapest_subscription_model():
    codex = build_classifier(
        settings_for('codex-subscription', acknowledge_subscription_risk=True), Repository())
    assert codex.model == 'gpt-6-luna'
    claude = build_classifier(
        settings_for('claude-subscription', acknowledge_subscription_risk=True), Repository())
    assert claude.model == 'claude-haiku-5-5'


def test_a_configured_model_is_used_as_is():
    classifier = build_classifier(
        settings_for('codex-subscription', acknowledge_subscription_risk=True,
                     model='chosen-custom-model'), Repository())
    assert classifier.model == 'chosen-custom-model'


def test_the_minimax_preset_keeps_its_own_default(monkeypatch):
    monkeypatch.setenv('SAID_DONE_API_KEY_DEFAULT', 'k')
    classifier = build_classifier(settings_for('minimax'), Repository())
    assert classifier.model == 'MiniMax-M3.1-Flash'


def test_the_minimax_placeholder_default_stays_accepted(monkeypatch):
    monkeypatch.setenv('SAID_DONE_API_KEY_DEFAULT', 'k')
    classifier = build_classifier(settings_for('minimax', model='default'), Repository())
    assert classifier.model == 'default'


@pytest.mark.parametrize('kind', ['openai-compatible', 'anthropic'])
def test_an_api_classifier_without_a_model_is_a_configuration_error(kind):
    with pytest.raises(ValueError) as error:
        resolve_screening_model(settings_for(kind).classifier)
    assert 'list_models' in str(error.value)


@pytest.mark.parametrize('kind', ['openai-compatible', 'anthropic'])
def test_the_placeholder_default_is_not_sent_to_an_api_provider(kind):
    with pytest.raises(ValueError):
        resolve_screening_model(settings_for(kind, model='default').classifier)


def test_a_configured_api_model_is_used_as_is():
    assert resolve_screening_model(
        settings_for('openai-compatible', model='gpt-5-mini').classifier) == 'gpt-5-mini'


@pytest.mark.parametrize('kind', ['openai-compatible', 'anthropic'])
def test_the_error_comes_before_the_provider_and_the_credential(kind, monkeypatch):
    """No provider is built and no key is looked up for a missing model."""
    import said_done.credentials
    import said_done.providers

    def forbidden(*args, **kwargs):
        raise AssertionError('the provider must not be built without a model')

    monkeypatch.setattr(said_done.providers, 'build_model_provider', forbidden)
    monkeypatch.setattr(said_done.credentials, 'get_api_key', forbidden)
    with pytest.raises(ValueError):
        build_classifier(settings_for(kind), Repository())


# ------------------------------------------------------ API catalogues ------
def _catalogue(cls, payload, base_url):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    return cls(api_key=API_KEY, base_url=base_url, client=client).list_models()


def test_an_api_catalogue_is_never_ranked():
    """An API catalogue stays unranked: the agent asks the user there."""
    payload = {'data': [{'id': 'vendor-large'}, {'id': 'vendor-small'}, {'id': 'vendor-mini'}]}
    models = _catalogue(OpenAICompatibleProvider, payload, 'https://api.openai.com/v1')
    assert [model.id for model in models] == ['vendor-large', 'vendor-small', 'vendor-mini']
    assert all(model.recommended_for_screening is None for model in models)


# ------------------------------------------------------------- executors ---
def test_the_executors_keep_no_default_model():
    """Execution writes real files: its model stays the user's choice."""
    from said_done.executor.config import CliExecutorConfig

    assert CliExecutorConfig().model is None


def test_the_screening_default_never_leaks_into_the_executor_configuration():
    from said_done.config import ExecutorSettings

    assert ExecutorSettings().model is None
    assert 'gpt-6-luna' not in json.dumps(ExecutorSettings().model_dump(mode='json'))
