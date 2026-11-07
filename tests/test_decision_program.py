"""Tests for the decision-program response mode.

The decision program is exercised directly with ``sys.executable`` and the
provider is driven by the fake agent in ``tests/fake_codex_agent.py``.  No
network, no real CLI, no git mutation and no user data are involved.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from said_done.config import GateConfig
from said_done.contracts import SemanticResponse, TranscriptBatch, TranscriptSegment
from said_done.providers import ProviderError
from said_done.providers.decision_program import (build_decision_prompt, build_smoke_prompt,
                                            create_decision_program, read_final_answer)
from said_done.semantic import factory as semantic_factory
from said_done.semantic.gate import ClassificationError, SemanticGate

FAKE_AGENT = Path(__file__).resolve().parent / 'fake_codex_agent.py'
TRANSCRIPT = '请帮我把这份 XRD 数据整理成报告大纲，并说明衍射原理。'
MARKER = 'SECRET-TRANSCRIPT-MARKER-9931'


# ------------------------------------------------------------- helpers -----
def shlex_split(command: str) -> list[str]:
    import shlex
    return shlex.split(command)


def make_agent(tmp_path: Path) -> Path:
    """A launcher so the provider can run the fake agent as its CLI."""
    launcher = tmp_path / 'fake-codex-agent'
    launcher.write_text(
        '#!' + sys.executable + '\n'
        'import runpy\n'
        'runpy.run_path(' + repr(str(FAKE_AGENT)) + ', run_name="__main__")\n',
        encoding='utf-8')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def make_context(text: str = TRANSCRIPT, known_tasks=None, gate=None) -> dict:
    now = datetime.now(timezone.utc)
    batch = TranscriptBatch(batch_id='batch-1', segments=[TranscriptSegment(
        segment_id='seg-1', timestamp_start=now, timestamp_end=now, text=text,
        source='test')])
    return {
        'batch': batch.model_dump(mode='json'),
        'known_tasks': known_tasks if known_tasks is not None else [],
        'gate': (gate or GateConfig()).model_dump(mode='json'),
    }


def make_provider(tmp_path: Path, **kwargs):
    from said_done.providers import CodexSubscriptionProvider
    params = {'acknowledge_subscription_risk': True, 'executable': str(make_agent(tmp_path)),
              'timeout': 120}
    params.update(kwargs)
    return CodexSubscriptionProvider(**params)


def make_program(tmp_path: Path, context: dict | None = None, name: str = 'program') -> Path:
    workdir = tmp_path / name
    create_decision_program(workdir, context if context is not None else make_context())
    return workdir


def run_decide(workdir: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(workdir / 'decide.py'), *args],
                          capture_output=True, text=True, timeout=timeout, cwd=str(workdir))


def run_yn(workdir: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(workdir / 'yn.py'), *args],
                          capture_output=True, text=True, timeout=timeout, cwd=str(workdir))


def valid_draft(context: dict) -> dict:
    segment = context['batch']['segments'][0]
    return {'decisions': [{'action': 'APPROVE', 'reason': '目标明确', 'task': {
        'task_type': 'CREATE_DOCUMENT', 'goal': '生成 XRD 报告大纲',
        'evidence_segment_ids': [segment['segment_id']],
        'original_evidence': [segment['text'][:30]],
        'expected_outputs': ['xrd_outline.docx'], 'dedup_key': 'xrd-outline'}}]}


def agent_env(tmp_path: Path, monkeypatch, scenario: str, **extra) -> dict:
    """Configure the fake agent and return the paths it records."""
    record = {'argv': tmp_path / 'argv.json', 'prompt': tmp_path / 'prompt.txt',
              'capture': tmp_path / 'capture'}
    monkeypatch.setenv('SAID_DONE_FAKE_SCENARIO', scenario)
    monkeypatch.setenv('SAID_DONE_FAKE_ARGV_FILE', str(record['argv']))
    monkeypatch.setenv('SAID_DONE_FAKE_PROMPT_FILE', str(record['prompt']))
    monkeypatch.setenv('SAID_DONE_FAKE_CAPTURE_DIR', str(record['capture']))
    for key, value in extra.items():
        monkeypatch.setenv(key, str(value))
    return record


def recorded_argv(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / 'argv.json').read_text(encoding='utf-8'))['argv']


# --------------------------------------------- the program on disk --------
def test_the_program_directory_holds_every_helper(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    for name in ('decide.py', 'yn.py', 'context.json', 'schema.json'):
        assert (workdir / name).is_file()
    assert json.loads((workdir / 'context.json').read_text(encoding='utf-8')) == context
    schema = json.loads((workdir / 'schema.json').read_text(encoding='utf-8'))
    assert schema == SemanticResponse.model_json_schema()


def test_the_decision_program_imports_the_project_package(tmp_path):
    workdir = make_program(tmp_path)
    source = (workdir / 'decide.py').read_text(encoding='utf-8')
    assert 'sys.path.insert(0, REPO_ROOT)' in source
    assert str(Path(__file__).resolve().parents[1]) in source


# ----------------------------------------------------- decide.py: N -------
def test_n_without_a_reason_records_no_decision(tmp_path):
    workdir = make_program(tmp_path)
    result = run_decide(workdir, 'N', '')
    assert result.returncode == 0
    assert 'ACCEPTED' in result.stdout
    assert json.loads((workdir / 'final.json').read_text(encoding='utf-8')) == {'decisions': []}


def test_n_with_a_reason_records_a_single_ignore(tmp_path):
    workdir = make_program(tmp_path)
    result = run_decide(workdir, 'N', '转写内容里没有可执行的任务')
    assert result.returncode == 0
    assert 'ACCEPTED' in result.stdout
    payload = json.loads((workdir / 'final.json').read_text(encoding='utf-8'))
    assert payload == {'decisions': [{'action': 'IGNORE', 'reason': '转写内容里没有可执行的任务'}]}


def test_n_truncates_a_long_reason(tmp_path):
    workdir = make_program(tmp_path)
    result = run_decide(workdir, 'N', '理' * 900)
    assert result.returncode == 0
    payload = json.loads((workdir / 'final.json').read_text(encoding='utf-8'))
    assert len(payload['decisions'][0]['reason']) == 500


def test_usage_errors_exit_64(tmp_path):
    workdir = make_program(tmp_path)
    for args in ((), ('N',), ('X', 'why'), ('submit',), ('N', 'a', 'b')):
        result = run_decide(workdir, *args)
        assert result.returncode == 64, args
        assert 'Usage' in result.stdout
    assert not (workdir / 'final.json').exists()


# -------------------------------------------------- decide.py: submit -----
def test_a_valid_draft_is_accepted(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps(valid_draft(context), ensure_ascii=False), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 0
    assert 'ACCEPTED' in result.stdout
    response = SemanticResponse.model_validate_json(
        (workdir / 'final.json').read_text(encoding='utf-8'))
    assert response.decisions[0].action == 'APPROVE'
    assert response.decisions[0].task.dedup_key == 'xrd-outline'


def test_an_invalid_draft_prints_a_hint_and_writes_no_answer(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE'}]}), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert not (workdir / 'final.json').exists()
    assert 'ValidationError' in result.stdout
    assert "'decisions', 0" in result.stdout
    assert 'Field required' in result.stdout
    assert (workdir / 'attempts.txt').read_text(encoding='utf-8') == '1'


def test_a_draft_without_a_task_is_pointed_at_the_task_field(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE', 'reason': 'r'}]}),
                      encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert 'APPROVE requires task' in result.stdout
    assert not (workdir / 'final.json').exists()


def test_a_broken_draft_file_is_reported_without_a_traceback(tmp_path):
    workdir = make_program(tmp_path)
    draft = workdir / 'draft.json'
    draft.write_text('{not json', encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert 'Traceback' not in result.stdout + result.stderr
    assert not (workdir / 'final.json').exists()


def test_the_draft_is_accepted_once_it_is_fixed(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE'}]}), encoding='utf-8')
    assert run_decide(workdir, 'submit', str(draft)).returncode == 1
    draft.write_text(json.dumps(valid_draft(context), ensure_ascii=False), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 0
    assert 'ACCEPTED' in result.stdout
    assert SemanticResponse.model_validate_json(
        (workdir / 'final.json').read_text(encoding='utf-8'))


# --------------------------------- the program uses the production rules ---
def test_the_program_rejects_evidence_that_is_not_in_the_transcript(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE', 'reason': 'r', 'task': {
        'task_type': 'CREATE_DOCUMENT', 'goal': '生成 XRD 报告大纲',
        'evidence_segment_ids': ['seg-1'], 'original_evidence': ['转写里没有的引文'],
        'expected_outputs': ['xrd_outline.docx'], 'dedup_key': 'xrd-outline'}}]}),
        encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert 'original transcript quote' in result.stdout
    assert not (workdir / 'final.json').exists()


def test_the_program_rejects_a_task_type_the_gate_forbids(tmp_path):
    gate = GateConfig(allowed_task_types=['CREATE_DOCUMENT'])
    context = make_context(gate=gate)
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE', 'reason': 'r', 'task': {
        'task_type': 'WRITE_CODE', 'goal': '写一个 XRD 脚本',
        'evidence_segment_ids': ['seg-1'], 'original_evidence': [TRANSCRIPT[:10]],
        'expected_outputs': ['xrd.py'], 'dedup_key': 'xrd-script'}}]}), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert 'not allowed by gate policy' in result.stdout
    assert not (workdir / 'final.json').exists()


def test_the_program_rejects_a_control_signal_for_an_unknown_task(tmp_path):
    context = make_context(known_tasks=[{'dedup_key': 'known-task'}])
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'CANCEL', 'reason': '取消',
                                                 'target_dedup_key': 'invented-key'}]}),
                      encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    assert 'invents a task identity' in result.stdout
    assert not (workdir / 'final.json').exists()


def test_the_program_accepts_a_control_signal_for_a_known_task(tmp_path):
    text = '取消之前的 XRD 报告任务'
    context = make_context(text=text, known_tasks=[{'dedup_key': 'known-task'}])
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'CANCEL', 'reason': '取消',
                                                 'target_dedup_key': 'known-task'}]}),
                      encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 0, result.stdout
    assert 'ACCEPTED' in result.stdout


# ------------------------------------------------------- attempt cap ------
def test_five_failed_submits_stop_the_program(tmp_path):
    context = make_context()
    workdir = make_program(tmp_path, context)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE'}]}), encoding='utf-8')
    for _ in range(5):
        assert run_decide(workdir, 'submit', str(draft)).returncode == 1
    assert (workdir / 'attempts.txt').read_text(encoding='utf-8') == '5'
    blocked = run_decide(workdir, 'submit', str(draft))
    assert blocked.returncode == 2
    assert 'ATTEMPT LIMIT REACHED' in blocked.stdout
    assert not (workdir / 'final.json').exists()


def test_the_attempt_cap_also_stops_the_no_task_path(tmp_path):
    workdir = make_program(tmp_path)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE'}]}), encoding='utf-8')
    for _ in range(5):
        assert run_decide(workdir, 'submit', str(draft)).returncode == 1
    blocked = run_decide(workdir, 'N', 'too late')
    assert blocked.returncode == 2
    assert 'ATTEMPT LIMIT REACHED' in blocked.stdout
    assert not (workdir / 'final.json').exists()


# ------------------------------------------------------ privacy of hints --
@pytest.mark.parametrize('failing_draft', [
    {'decisions': [{'action': 'APPROVE'}]},
    {'decisions': [{'action': 'APPROVE', 'reason': 'r', 'task': {
        'task_type': 'CREATE_DOCUMENT', 'goal': '生成 XRD 报告大纲',
        'evidence_segment_ids': ['seg-1'], 'original_evidence': ['转写里没有的引文'],
        'expected_outputs': ['xrd_outline.docx'], 'dedup_key': 'xrd-outline'}}]},
    {'decisions': [{'action': 'CANCEL', 'reason': '取消', 'target_dedup_key': 'invented-key'}]},
    {'decisions': 'not a list'},
])
def test_a_fix_hint_never_repeats_the_transcript(tmp_path, failing_draft):
    text = f'{MARKER} {TRANSCRIPT}'
    workdir = make_program(tmp_path, make_context(text=text))
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps(failing_draft, ensure_ascii=False), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert MARKER not in output
    assert TRANSCRIPT not in output
    assert not any(window in output for window in
                   (text[i:i + 12] for i in range(0, len(text) - 12)))


def test_a_fix_hint_stays_short_and_points_at_the_field(tmp_path):
    workdir = make_program(tmp_path)
    draft = workdir / 'draft.json'
    draft.write_text(json.dumps({'decisions': [{'action': 'APPROVE'}]}), encoding='utf-8')
    result = run_decide(workdir, 'submit', str(draft))
    lines = result.stdout.splitlines()
    assert 0 < len(lines) <= 20
    assert all(len(line) <= 400 for line in lines)
    assert 'decisions' in result.stdout


# -------------------------------------------------------------- yn.py -----
def test_yn_answers_yes(tmp_path):
    workdir = make_program(tmp_path)
    result = run_yn(workdir, 'Y')
    assert result.returncode == 0
    assert 'OK' in result.stdout
    assert (workdir / 'answer.txt').read_text(encoding='utf-8') == 'Y'


def test_yn_answers_no(tmp_path):
    workdir = make_program(tmp_path)
    result = run_yn(workdir, 'N')
    assert result.returncode == 0
    assert (workdir / 'answer.txt').read_text(encoding='utf-8') == 'N'


@pytest.mark.parametrize('args', [(), ('maybe',), ('Y', 'N')])
def test_yn_refuses_anything_else(tmp_path, args):
    workdir = make_program(tmp_path)
    result = run_yn(workdir, *args)
    assert result.returncode == 64
    assert not (workdir / 'answer.txt').exists()


# --------------------------------------------------- the final reader -----
def test_the_final_reader_ignores_a_missing_file(tmp_path):
    assert read_final_answer(tmp_path) == ''


def test_the_final_reader_refuses_an_oversized_file(tmp_path):
    (tmp_path / 'final.json').write_text('x' * (1024 * 1024 + 1), encoding='utf-8')
    with pytest.raises(ProviderError) as error:
        read_final_answer(tmp_path)
    assert error.value.error_type == 'OutputLimit'


def test_the_final_reader_refuses_a_symlink(tmp_path):
    target = tmp_path / 'target.json'
    target.write_text('{"decisions": []}', encoding='utf-8')
    (tmp_path / 'final.json').symlink_to(target)
    assert read_final_answer(tmp_path) == ''


# ------------------------------------------------------- the prompts ------
def test_the_decision_prompt_offers_absolute_commands(tmp_path):
    workdir = make_program(tmp_path)
    prompt = build_decision_prompt([{'role': 'user', 'content': '转写内容'}], workdir)
    assert str((workdir / 'decide.py').resolve()) in prompt
    assert str((workdir / 'schema.json').resolve()) in prompt
    assert str((workdir / 'draft.json').resolve()) in prompt
    assert 'context.json' in prompt
    assert 'ACCEPTED' in prompt
    assert 'Never modify decide.py, context.json or schema.json.' in prompt
    commands = [shlex_split(command) for command in prompt.split('`')[1::2]]
    decide = [parts for parts in commands if parts[1].endswith('decide.py')]
    assert len(decide) == 2
    assert decide[0][0] == sys.executable
    assert decide[0][2] == 'N'
    assert decide[1][2] == 'submit'


def test_the_smoke_prompt_offers_only_the_yn_program(tmp_path):
    workdir = make_program(tmp_path)
    prompt = build_smoke_prompt(workdir)
    assert '2 + 2' in prompt
    commands = [shlex_split(command) for command in prompt.split('`')[1::2]]
    assert len(commands) == 2
    assert all(parts[1].endswith('yn.py') for parts in commands)
    assert {parts[2] for parts in commands} == {'Y', 'N'}
    assert all(parts[0] == sys.executable for parts in commands)


def test_a_program_path_with_spaces_is_quoted(tmp_path):
    workdir = make_program(tmp_path, name='space dir')
    prompt = build_decision_prompt([{'role': 'user', 'content': 'x'}], workdir)
    commands = [shlex_split(command) for command in prompt.split('`')[1::2]]
    for parts in commands:
        assert parts[1] == str((workdir / 'decide.py').resolve())
    smoke = build_smoke_prompt(workdir)
    for command in smoke.split('`')[1::2]:
        assert shlex_split(command)[1] == str((workdir / 'yn.py').resolve())


# ------------------------------------------------- the provider, E2E ------
def test_program_mode_records_a_no_task_decision(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'n')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    payload = json.loads(answer)
    assert payload['decisions'][0]['action'] == 'IGNORE'
    assert payload['decisions'][0]['reason']


def test_program_mode_records_an_empty_decision_without_a_reason(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'n_no_reason')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    assert json.loads(answer) == {'decisions': []}


def test_program_mode_returns_the_validated_decision(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'valid_submit')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    response = SemanticResponse.model_validate_json(answer)
    assert response.decisions[0].action == 'APPROVE'
    assert response.decisions[0].task.expected_outputs == ['xrd_outline.docx']


def test_program_mode_recovers_from_a_rejected_draft(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'invalid_then_fixed')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    assert SemanticResponse.model_validate_json(answer).decisions[0].action == 'APPROVE'


def test_program_mode_reports_a_missing_decision(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'never')
    provider = make_provider(tmp_path)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                          effort=None, json_schema=None, max_output_tokens=100,
                          validation_context=make_context())
    assert error.value.error_type == 'NoDecision'
    assert error.value.retryable is True


def test_program_mode_ignores_what_the_model_says_in_prose(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'chatty')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    assert 'seems actionable' not in answer
    assert SemanticResponse.model_validate_json(answer).decisions[0].action == 'APPROVE'


def test_program_mode_reports_the_attempt_limit(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'attempt_limit')
    provider = make_provider(tmp_path)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                          effort=None, json_schema=None, max_output_tokens=100,
                          validation_context=make_context())
    assert error.value.error_type == 'DecisionAttemptLimit'
    assert error.value.retryable is True


def test_program_mode_runs_the_cli_in_the_program_directory(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'n')
    provider = make_provider(tmp_path)
    provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                      effort=None, json_schema=None, max_output_tokens=100,
                      validation_context=make_context())
    argv = recorded_argv(tmp_path)
    assert argv[0] == 'exec'
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert '--skip-git-repo-check' in argv
    assert '--ephemeral' in argv
    assert argv[-1] == '-'
    # the answer file of the legacy mode is not used in program mode
    assert '-o' not in argv
    workdir = argv[argv.index('-C') + 1]
    # the program directory is throw-away: it is gone once the call is over,
    # so the agent's copy is what can be inspected afterwards
    assert not Path(workdir).exists()
    assert (tmp_path / 'capture' / 'decide.py').is_file()
    assert (tmp_path / 'capture' / 'context.json').is_file()
    assert json.loads((tmp_path / 'capture' / 'context.json').read_text(encoding='utf-8'))['batch']
    # the agent really ran inside the directory the provider named
    recorded = json.loads((tmp_path / 'argv.json').read_text(encoding='utf-8'))
    assert Path(recorded['cwd']).resolve() == Path(workdir).resolve()


def test_program_mode_carries_the_model_and_the_effort(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'n')
    provider = make_provider(tmp_path)
    provider.complete([{'role': 'user', 'content': '转写内容'}], model='gpt-6-luna',
                      effort='low', json_schema=None, max_output_tokens=100,
                      validation_context=make_context())
    argv = recorded_argv(tmp_path)
    assert argv[argv.index('-m') + 1] == 'gpt-6-luna'
    assert argv[argv.index('-c') + 1] == 'model_reasoning_effort="low"'


def test_program_mode_omits_the_default_model_and_a_missing_effort(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'n')
    provider = make_provider(tmp_path)
    provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                      effort=None, json_schema=None, max_output_tokens=100,
                      validation_context=make_context())
    argv = recorded_argv(tmp_path)
    assert '-m' not in argv
    assert '-c' not in argv


def test_the_final_message_mode_is_unchanged(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'final_message')
    provider = make_provider(tmp_path, response_mode='final-message')
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100)
    assert json.loads(answer) == {'decisions': []}
    argv = recorded_argv(tmp_path)
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '-o' in argv
    assert '-C' not in argv
    assert not (tmp_path / 'capture' / 'decide.py').exists()


def test_the_final_message_mode_ignores_an_explicit_context(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'final_message')
    provider = make_provider(tmp_path, response_mode='final-message')
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100,
                                validation_context=make_context())
    assert json.loads(answer) == {'decisions': []}
    argv = recorded_argv(tmp_path)
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '-o' in argv
    assert '-C' not in argv
    assert not (tmp_path / 'capture' / 'decide.py').exists()


def test_a_cli_failure_keeps_the_code_and_drops_the_message(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'cli_error')
    provider = make_provider(tmp_path)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                          effort=None, json_schema=None, max_output_tokens=100,
                          validation_context=make_context())
    assert error.value.error_type == 'CliExit'
    assert error.value.retryable is True
    assert 'invalid_json_schema' in str(error.value)
    assert 'PRIVATE' not in str(error.value)
    assert 'transcript text' not in str(error.value)


def test_program_mode_without_a_context_keeps_the_legacy_path(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'final_message')
    provider = make_provider(tmp_path)
    answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                effort=None, json_schema=None, max_output_tokens=100)
    assert json.loads(answer) == {'decisions': []}
    argv = recorded_argv(tmp_path)
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '-C' not in argv


def test_program_mode_works_when_the_directory_has_a_space(tmp_path, monkeypatch):
    space = tmp_path / 'space dir'
    space.mkdir()
    monkeypatch.setenv('TMPDIR', str(space))
    original = tempfile.tempdir
    tempfile.tempdir = None
    try:
        agent_env(tmp_path, monkeypatch, 'valid_submit')
        provider = make_provider(tmp_path)
        answer = provider.complete([{'role': 'user', 'content': '转写内容'}], model='default',
                                    effort=None, json_schema=None, max_output_tokens=100,
                                    validation_context=make_context())
    finally:
        tempfile.tempdir = original
    assert SemanticResponse.model_validate_json(answer).decisions[0].action == 'APPROVE'
    argv = recorded_argv(tmp_path)
    assert ' ' in argv[argv.index('-C') + 1]


# ------------------------------------------------------- the smoke probe --
def test_the_smoke_probe_answers_yes(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'smoke_y')
    provider = make_provider(tmp_path)
    result = provider.smoke_test()
    assert result['ok'] is True
    assert result['answer'] == 'Y'
    assert result['error'] is None
    assert isinstance(result['seconds'], float)


def test_the_smoke_probe_reports_a_wrong_answer(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'smoke_n')
    provider = make_provider(tmp_path)
    result = provider.smoke_test()
    assert result['ok'] is False
    assert result['answer'] == 'N'
    assert result['error'] == 'IncorrectAnswer'


def test_the_smoke_probe_reports_a_missing_answer(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'smoke_never')
    provider = make_provider(tmp_path)
    result = provider.smoke_test()
    assert result['ok'] is False
    assert result['answer'] is None
    assert result['error'] == 'NoAnswer'


def test_the_smoke_probe_runs_in_the_program_directory(tmp_path, monkeypatch):
    agent_env(tmp_path, monkeypatch, 'smoke_y')
    provider = make_provider(tmp_path)
    provider.smoke_test()
    argv = recorded_argv(tmp_path)
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    workdir = argv[argv.index('-C') + 1]
    assert not Path(workdir).exists()
    assert (tmp_path / 'capture' / 'yn.py').is_file()
    assert (tmp_path / 'capture' / 'answer.txt').read_text(encoding='utf-8') == 'Y'
    recorded = json.loads((tmp_path / 'argv.json').read_text(encoding='utf-8'))
    assert Path(recorded['cwd']).resolve() == Path(workdir).resolve()


# ------------------------------------------------------- the gate ---------
class _PlainProvider:
    """A provider whose complete() has no validation_context parameter."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def complete(self, messages, *, model, effort, json_schema, max_output_tokens):
        self.calls += 1
        return self.content

    def close(self) -> None:
        pass


class _ContextProvider:
    """A provider which accepts the validation context."""

    supports_validation_context = True

    def __init__(self, content: str) -> None:
        self.content = content
        self.contexts = []

    def complete(self, messages, *, model, effort, json_schema, max_output_tokens,
                 validation_context=None):
        self.contexts.append(validation_context)
        return self.content

    def close(self) -> None:
        pass


def _gate(provider, **kwargs) -> SemanticGate:
    return SemanticGate(provider, model='default', gate=GateConfig(), max_attempts=1,
                        sleep=lambda seconds: None, **kwargs)


def _batch() -> TranscriptBatch:
    now = datetime.now(timezone.utc)
    return TranscriptBatch(batch_id='batch-1', segments=[TranscriptSegment(
        segment_id='seg-1', timestamp_start=now, timestamp_end=now, text=TRANSCRIPT,
        source='test')])


def test_the_gate_passes_the_validation_context_to_a_supporting_provider():
    provider = _ContextProvider('{"decisions": []}')
    gate = _gate(provider)
    gate.classify(_batch())
    context = provider.contexts[0]
    assert set(context) == {'batch', 'known_tasks', 'gate'}
    assert context['batch']['segments'][0]['segment_id'] == 'seg-1'
    assert context['known_tasks'] is None
    assert context['gate']['allowed_task_types'] == GateConfig().allowed_task_types


def test_the_gate_does_not_pass_the_context_to_other_providers():
    provider = _PlainProvider('{"decisions": []}')
    gate = _gate(provider)
    response = gate.classify(_batch())
    assert response.decisions == []
    assert provider.calls == 1


def test_the_gate_still_rejects_output_the_program_never_wrote():
    provider = _ContextProvider('I think you should approve this one')
    gate = _gate(provider)
    with pytest.raises(ClassificationError):
        gate.classify(_batch())
    assert provider.contexts  # the context was still offered


def test_the_gate_still_rejects_a_semantically_invalid_response():
    content = json.dumps({'decisions': [{'action': 'APPROVE', 'reason': 'r', 'task': {
        'task_type': 'CREATE_DOCUMENT', 'goal': '生成 XRD 报告大纲',
        'evidence_segment_ids': ['seg-unknown'], 'original_evidence': [TRANSCRIPT[:10]],
        'expected_outputs': ['xrd_outline.docx'], 'dedup_key': 'xrd-outline'}}]})
    provider = _ContextProvider(content)
    gate = _gate(provider)
    with pytest.raises(ClassificationError):
        gate.classify(_batch())


# ---------------------------------------------------- the classifier ------
class _StubClassifier:
    def __init__(self, provider, content='{"decisions": []}') -> None:
        self.provider = provider
        self.model = 'stub-model'
        self.content = content
        self.calls = 0
        self.order: list[str] = []

    def classify(self, batch):
        self.calls += 1
        return SemanticResponse.model_validate(json.loads(self.content))

    def close(self) -> None:
        self.order.append('close')


class _SmokeProvider:
    def __init__(self, result, order=None) -> None:
        self.result = result
        self.order = order if order is not None else []

    def smoke_test(self) -> dict:
        self.order.append('smoke')
        return self.result

    def close(self) -> None:
        pass


class _ProviderWithoutSmoke:
    """A provider which has no smoke probe at all."""

    def close(self) -> None:
        pass


def _settings():
    from said_done.config import Settings
    return Settings()


def test_the_classifier_smokes_the_program_before_classifying(monkeypatch):
    order: list[str] = []
    provider = _SmokeProvider({'ok': True, 'answer': 'Y', 'seconds': 0.1, 'error': None}, order)
    classifier = _StubClassifier(provider)
    classifier.order = order
    monkeypatch.setattr(semantic_factory, 'build_classifier', lambda settings, repository: classifier)
    result = semantic_factory.test_classifier(_settings())
    assert result['ok'] is True
    assert result['program_smoke']['ok'] is True
    assert result['actions'] == []
    assert result['decision_count'] == 0
    assert order == ['smoke', 'close']
    assert classifier.calls == 1


def test_the_classifier_skips_the_classification_when_the_smoke_fails(monkeypatch):
    provider = _SmokeProvider({'ok': False, 'answer': None, 'seconds': 0.1, 'error': 'NoAnswer'})
    classifier = _StubClassifier(provider)
    monkeypatch.setattr(semantic_factory, 'build_classifier', lambda settings, repository: classifier)
    result = semantic_factory.test_classifier(_settings())
    assert result['ok'] is False
    assert result['program_smoke']['ok'] is False
    assert result['program_smoke']['error'] == 'NoAnswer'
    assert 'actions' not in result
    assert 'decision_count' not in result
    assert classifier.calls == 0


def test_a_provider_without_a_smoke_probe_has_no_smoke_result(monkeypatch):
    classifier = _StubClassifier(_ProviderWithoutSmoke())
    monkeypatch.setattr(semantic_factory, 'build_classifier', lambda settings, repository: classifier)
    result = semantic_factory.test_classifier(_settings())
    assert result['ok'] is True
    assert 'program_smoke' not in result
    assert classifier.calls == 1
