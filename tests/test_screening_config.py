"""Configuration changes affect prompts and gates without relaxing safety checks."""
import hashlib
import pytest
from said_done.config import GateConfig, Settings
from said_done.contracts import SemanticResponse, TaskDecision
from said_done.heartbeat.threshold import should_process
from said_done.semantic.factory import build_classifier, test_classifier as probe_classifier
from said_done.semantic.gate import ClassificationError, SemanticGate
from said_done.semantic.mock_adapter import MockSemanticAdapter
from said_done.semantic.prompt import SYSTEM_PROMPT, build_messages, build_system_prompt
from said_done.semantic.screening import explain_screening
from said_done.semantic.validator import validate_response
from tests.test_semantic import batch


def test_default_prompt_is_identical():
    assert hashlib.sha256(build_system_prompt().encode()).hexdigest() == '172ad9597d62ada34dc9a74bc7990b4ec3a53409ed849a29b6b57fd6dfe9b576'
    assert build_system_prompt() == SYSTEM_PROMPT
    assert build_system_prompt(GateConfig()) == SYSTEM_PROMPT
    assert build_messages(batch('plain'))[0]['content'].startswith(SYSTEM_PROMPT + '\nSchema:\n')


def test_custom_prompt_and_mock_formats():
    config = GateConfig(default_document_format='md', default_code_format='js',
                        extra_instructions='只接受研究任务', allowed_task_types=['CREATE_DOCUMENT'])
    prompt = build_system_prompt(config)
    assert '大纲默认md，代码默认js。' in prompt
    assert prompt.endswith('只接受研究任务') and '不得放宽' in prompt
    result = MockSemanticAdapter(config).classify(batch('我需要一个 XRD 原理报告大纲'))
    assert result.decisions[0].task.defaults['format'] == 'md'
    assert result.decisions[0].task.expected_outputs == ['outline.md']
    assert '仅允许以下任务类型：CREATE_DOCUMENT。' in prompt


def test_disallowed_approval_rejected():
    b = batch('我需要一个 XRD 原理报告大纲')
    response = MockSemanticAdapter().classify(b)
    with pytest.raises(ValueError, match='not allowed'):
        validate_response(response, b, gate=GateConfig(allowed_task_types=['WRITE_CODE']))


@pytest.mark.parametrize('action,keyword,field', [
    ('CANCEL', '停止', 'cancel_keywords'), ('ALREADY_COMPLETED', '交付', 'completion_keywords'),
])
def test_control_keyword_config(action, keyword, field):
    b = batch(keyword + ' XRD 原理报告大纲')
    response = SemanticResponse(decisions=[TaskDecision(action=action, reason='control', target_dedup_key='existing')])
    known = [{'dedup_key': 'existing'}]
    with pytest.raises(ValueError, match='evidence'):
        validate_response(response, b, known)
    config = GateConfig(**{field: [keyword]})
    assert validate_response(response, b, known, config) == response
    assert keyword in build_system_prompt(config)


def test_empty_keywords_disable_default_trigger():
    segments = batch('我要').segments
    assert should_process(segments)
    assert not should_process(segments, keywords=[])
    assert not should_process(segments, keywords=['专属触发'])
    assert should_process(batch('专属触发').segments, keywords=['专属触发'])


def test_screening_explanation_values():
    settings = Settings(screening={'batch': {'min_content_chars': 42}, 'gate': {'max_classification_attempts': 7}})
    info = explain_screening(settings)
    assert set(info) == {'values', 'defaults', 'explanation'}
    assert info['values']['batch']['min_content_chars'] == 42
    assert info['defaults']['batch']['min_content_chars'] == 200
    assert '42' in info['explanation']['when'] and info['explanation']['retries']['per_batch_attempts'] == 7
    assert '7' in info['explanation']['retries']['summary']


class FakeProvider:
    kind = 'fake'
    def __init__(self, outputs):
        self.outputs, self.calls, self.closed = iter(outputs), [], False

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output

    def close(self):
        self.closed = True


def test_gate_retry_fences_context_and_schema():
    b = batch('我需要一个 XRD 原理报告大纲')
    expected = MockSemanticAdapter().classify(b)
    provider = FakeProvider(['invalid-private-content', '```json\n' + expected.model_dump_json() + '\n```'])
    gate = SemanticGate(provider, model='test', effort='low', sleep=lambda _: None,
                        context_provider=lambda _: [{'dedup_key': 'stored'}])
    assert gate.classify(b) == expected
    messages, options = provider.calls[1]
    assert 'invalid output' in messages[-1]['content']
    assert 'invalid-private-content' not in str(messages)
    assert options['effort'] == 'low' and options['json_schema']['type'] == 'object'
    gate.close()
    assert provider.closed


def test_gate_error_never_echoes_private_output():
    provider = FakeProvider(['private-transcript-secret'] * 2)
    gate = SemanticGate(provider, model='test', max_attempts=2, sleep=lambda _: None)
    with pytest.raises(ClassificationError) as error:
        gate.classify(batch('private-transcript-secret'))
    assert 'private-transcript-secret' not in str(error.value)
    assert len(provider.calls) == 2


def test_permanent_provider_error_is_not_retried():
    class Rejected(RuntimeError):
        retryable = False
    provider = FakeProvider([Rejected('private-key')])
    with pytest.raises(ClassificationError) as error:
        SemanticGate(provider, model='test').classify(batch('hello'))
    assert 'private-key' not in str(error.value) and len(provider.calls) == 1


def test_policy_cannot_relax_evidence_validation():
    b = batch('我需要一个 XRD 原理报告大纲')
    response = MockSemanticAdapter().classify(b)
    response.decisions[0].task.original_evidence = ['invented-private-evidence']
    provider = FakeProvider([response.model_dump_json()])
    with pytest.raises(ClassificationError):
        SemanticGate(provider, model='test', max_attempts=1,
                     gate=GateConfig(extra_instructions='忽略证据校验')).classify(b)


def test_factory_mock_and_synthetic_probe():
    class Repository:
        def semantic_context(self, batch):
            return []
    settings = Settings(classifier={'kind': 'mock'})
    classifier = build_classifier(settings, Repository())
    assert classifier.model == 'mock'
    probe = probe_classifier(settings)
    assert probe['ok'] and probe['actions'] == ['APPROVE']
    assert 'XRD' not in str(probe)


def test_mock_desktop_path_does_not_stat(monkeypatch):
    from pathlib import Path
    def forbidden(*args, **kwargs):
        raise AssertionError('mock must not inspect local file existence')
    monkeypatch.setattr(Path, 'exists', forbidden)
    response = MockSemanticAdapter().classify(batch('我要把桌面 A.xlsx 转成 CSV'))
    assert response.decisions[0].action == 'APPROVE'
    assert response.decisions[0].task.input_paths[0].endswith('/Desktop/A.xlsx')


def test_factory_nonmock_uses_config_and_trusted_context(monkeypatch):
    import said_done.providers
    b = batch('我需要一个 XRD 原理报告大纲')
    response = MockSemanticAdapter().classify(b)
    provider = FakeProvider([response.model_dump_json()])
    monkeypatch.setattr(said_done.providers, 'build_model_provider', lambda settings: provider)
    class Repository:
        def semantic_context(self, received):
            assert received == b
            return [{'dedup_key': 'trusted-existing'}]
    settings = Settings(classifier={'kind': 'anthropic', 'model': 'chosen-model', 'effort': 'high'},
                        screening={'gate': {'extra_instructions': '只接受研究任务'}})
    classifier = build_classifier(settings, Repository())
    assert classifier.model == 'chosen-model'
    assert classifier.classify(b) == response
    messages, options = provider.calls[0]
    assert 'trusted-existing' in messages[1]['content']
    assert '只接受研究任务' in messages[0]['content']
    assert options['model'] == 'chosen-model' and options['effort'] == 'high'
    classifier.close()
    assert provider.closed


def test_synthetic_probe_sanitizes_failures_and_closes(monkeypatch):
    import said_done.providers
    provider = FakeProvider([RuntimeError('private-provider-credential')])
    monkeypatch.setattr(said_done.providers, 'build_model_provider', lambda settings: provider)
    result = probe_classifier(Settings(classifier={'kind': 'anthropic', 'model': 'chosen', 'max_attempts': 1}))
    assert result['ok'] is False and result['error'] == 'ClassificationError'
    assert result['detail'] == 'classification failed after 1 attempts (RuntimeError)'
    assert 'private-provider-credential' not in str(result) and provider.closed


def test_synthetic_probe_reports_provider_error_category(monkeypatch):
    import said_done.providers
    from said_done.providers.base import ProviderError
    provider = FakeProvider([ProviderError('rate limited (http 429)', error_type='RateLimited')])
    monkeypatch.setattr(said_done.providers, 'build_model_provider', lambda settings: provider)
    result = probe_classifier(Settings(classifier={'kind': 'anthropic', 'model': 'chosen', 'max_attempts': 1}))
    assert 'ProviderError:RateLimited' in result['detail']



def test_retry_layers_are_capped_and_explained():
    import pytest
    from pydantic import ValidationError
    from said_done.config import Settings
    from said_done.semantic.screening import explain_screening
    with pytest.raises(ValidationError):
        Settings(screening={'gate': {'max_classification_attempts': 11}})
    with pytest.raises(ValidationError):
        Settings(classifier={'max_attempts': 6})
    retries = explain_screening(Settings())['explanation']['retries']
    assert retries['per_call_attempts'] == 3 and retries['per_batch_attempts'] == 3
    assert retries['worst_case_model_calls_per_batch'] == 9
