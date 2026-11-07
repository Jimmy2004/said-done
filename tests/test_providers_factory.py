"""Factory tests: provider selection, credential lookup, model lists, mock path.

No network, no keyring and no real CLI: the credential lookup is stubbed and
the only provider that runs is the mock one.
"""
import httpx
import pytest
from pydantic import SecretStr

from said_done.config import Settings
from said_done.contracts import TranscriptBatch, TranscriptSegment, utcnow
from said_done.providers import (AnthropicProvider, ClaudeSubscriptionProvider,
                           CodexSubscriptionProvider, MiniMaxProvider, MockProvider,
                           ChatCompletionsProvider, ModelProvider, OpenAICompatibleProvider, ProviderError,
                           SUBSCRIPTION_DISCLAIMER, SubscriptionRiskNotAcknowledged,
                           build_model_provider, list_models, resolve_api_style)


def settings_for(kind='mock', **classifier):
    payload = {'kind': kind}
    payload.update(classifier)
    return Settings(classifier=payload)


@pytest.fixture(autouse=True)
def no_stored_credentials(monkeypatch):
    monkeypatch.delenv('SAID_DONE_API_KEY_DEFAULT', raising=False)
    monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
    import said_done.credentials
    monkeypatch.setattr(said_done.credentials, 'get_api_key', lambda *args, **kwargs: None)


# ------------------------------------------------------------- selection ----
def test_mock_provider_is_selected_for_the_mock_kind():
    provider = build_model_provider(settings_for('mock'))
    assert isinstance(provider, MockProvider)
    assert provider.kind == 'mock'


@pytest.mark.parametrize('kind,base_url,expected', [
    ('openai-compatible', 'https://api.openai.com/v1', OpenAICompatibleProvider),
    ('anthropic', 'https://api.anthropic.com', AnthropicProvider),
    ('chat-completions', 'https://api.minimaxi.com/v1', ChatCompletionsProvider),
    ('anthropic', 'https://proxy.internal.example.com', AnthropicProvider),
    ('minimax', 'https://proxy.internal.example.com', MiniMaxProvider),
])
def test_api_style_is_resolved_from_the_kind_or_the_base_url(kind, base_url, expected):
    provider = build_model_provider(settings_for(kind, base_url=base_url), api_key='k')
    assert isinstance(provider, expected)
    provider.close()


def test_explicit_kind_wins_over_the_base_url():
    provider = build_model_provider(settings_for('anthropic', base_url='https://api.minimaxi.com/v1'),
                                    api_key='k')
    assert isinstance(provider, AnthropicProvider)
    provider.close()


def test_resolve_api_style_reads_an_explicit_api_style():
    """A configuration carrying an explicit style wins over the base url."""
    class Config:
        kind = 'openai-compatible'
        base_url = 'https://api.openai.com/v1'
        api_style = 'chat-completions'

    assert resolve_api_style(Config()) == 'chat-completions'


def test_minimax_provider_defaults_to_its_own_base_url_and_model():
    provider = build_model_provider(settings_for('minimax'), api_key='k')
    assert isinstance(provider, MiniMaxProvider)
    assert provider.base_url == 'https://api.minimaxi.com/v1'
    assert provider.resolve_model('default') == 'MiniMax-M3.1-Flash'
    provider.close()


# ---------------------------------------------------------- credentials ----
def test_the_api_key_is_kept_as_a_secret_str():
    provider = build_model_provider(settings_for('anthropic'), api_key='secret-value')
    assert isinstance(provider.api_key, SecretStr)
    assert 'secret-value' not in repr(provider.api_key)
    provider.close()


def test_the_environment_override_wins(monkeypatch):
    monkeypatch.setenv('SAID_DONE_API_KEY_DEFAULT', 'env-key')
    provider = build_model_provider(settings_for('anthropic'))
    assert provider.api_key.get_secret_value() == 'env-key'
    provider.close()


def test_a_missing_key_is_a_permanent_failure():
    with pytest.raises(ProviderError) as error:
        build_model_provider(settings_for('anthropic'))
    assert error.value.retryable is False
    assert error.value.error_type == 'MissingCredential'


def test_the_legacy_minimax_variable_is_still_accepted(monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'legacy-key')
    provider = build_model_provider(settings_for('minimax'))
    assert provider.api_key.get_secret_value() == 'legacy-key'
    provider.close()


# --------------------------------------------------------- subscriptions ----
def test_subscription_providers_need_the_acknowledgement():
    with pytest.raises(SubscriptionRiskNotAcknowledged) as error:
        build_model_provider(settings_for('codex-subscription'))
    assert error.value.retryable is False
    with pytest.raises(SubscriptionRiskNotAcknowledged):
        build_model_provider(settings_for('claude-subscription'))


def test_subscription_providers_are_built_once_acknowledged():
    settings = settings_for('codex-subscription', acknowledge_subscription_risk=True)
    assert isinstance(build_model_provider(settings), CodexSubscriptionProvider)
    settings = settings_for('claude-subscription', acknowledge_subscription_risk=True)
    assert isinstance(build_model_provider(settings), ClaudeSubscriptionProvider)


# ----------------------------------------------------------- model list ----
def test_list_models_returns_plain_dictionaries():
    models = list_models(settings_for('mock'))
    assert isinstance(models, list)
    assert models and isinstance(models[0], dict)
    assert models[0]['id'] == 'mock'
    assert 'display_name' in models[0] and 'supports_effort' in models[0]


def test_list_models_of_a_subscription_provider_needs_no_cli():
    models = list_models(settings_for('codex-subscription', acknowledge_subscription_risk=True))
    assert any(model['id'] == 'default' for model in models)


# --------------------------------------------------------------- mock path --
def test_the_mock_provider_serves_the_gate():
    from said_done.semantic.gate import SemanticGate

    settings = settings_for('mock')
    provider = build_model_provider(settings)
    gate = SemanticGate(provider, model='mock', effort=None, gate=settings.screening.gate,
                        max_attempts=2)
    now = utcnow()
    batch = TranscriptBatch(batch_id='b', segments=[TranscriptSegment(
        segment_id='1', timestamp_start=now, timestamp_end=now,
        text='我需要一个 XRD 原理报告大纲', source='test')])
    response = gate.classify(batch)
    assert response.decisions[0].action == 'APPROVE'
    assert response.decisions[0].task.evidence_segment_ids == ['1']
    gate.close()


def test_the_mock_provider_reads_a_batch_out_of_the_messages():
    from said_done.semantic.prompt import build_messages

    settings = settings_for('mock')
    provider = build_model_provider(settings)
    now = utcnow()
    batch = TranscriptBatch(batch_id='b', segments=[TranscriptSegment(
        segment_id='1', timestamp_start=now, timestamp_end=now,
        text='我要把桌面 A.xlsx 转成 CSV', source='test')])
    messages = build_messages(batch, None, settings.screening.gate)
    payload = provider.complete(messages, model='mock', effort=None, json_schema=None,
                                 max_output_tokens=100)
    assert '"APPROVE"' in payload


# ------------------------------------------------------------- contract -----
def test_every_provider_satisfies_the_protocol():
    settings = settings_for('mock')
    assert isinstance(build_model_provider(settings), ModelProvider)
    provider = build_model_provider(settings_for('anthropic'), api_key='k')
    assert isinstance(provider, ModelProvider)
    provider.close()


def test_the_disclaimer_is_bilingual_and_keeps_the_design_wording():
    assert '一切后果由你自行承担' in SUBSCRIPTION_DISCLAIMER
    assert 'All consequences are your own responsibility.' in SUBSCRIPTION_DISCLAIMER
    assert 'ChatGPT/Claude' in SUBSCRIPTION_DISCLAIMER


def test_a_provider_that_was_never_built_leaks_no_key():
    """A failed request must not carry the key into the error message."""
    def handler(request):
        return httpx.Response(401, json={'error': 'nope'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(api_key='super-secret', base_url='https://api.anthropic.com',
                                 client=client)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='claude-x', effort=None,
                           json_schema=None, max_output_tokens=10)
    assert 'super-secret' not in str(error.value)
