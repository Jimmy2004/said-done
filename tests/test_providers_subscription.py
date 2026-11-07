"""Subscription provider tests: CLI assembly, risk gate, timeout, bounded output.

The fake CLIs are generated inside this file; no real CLI, no network and no
keyring are touched.  The CLI flags are therefore only fake-tested.
"""
import json
import os
import stat
import sys
import time

import pytest

from said_done.providers import (CodexSubscriptionProvider, ClaudeSubscriptionProvider,
                           ProviderError, SubscriptionRiskNotAcknowledged)
from said_done.providers.base import SubscriptionRiskNotAcknowledged as _RiskError
from said_done.providers.cli_runner import MAX_OUTPUT
from said_done.providers.codex_subscription import read_bounded_answer

CODEX_FAKE = '''#!/usr/bin/env python3
import json, os, sys, time

args = sys.argv[1:]
record = os.environ.get("SAID_DONE_TEST_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(args, handle)
mode = os.environ.get("SAID_DONE_TEST_MODE", "ok")
sys.stdin.read()
if mode == "hang":
    pid = os.fork()
    if pid == 0:
        time.sleep(30)
        os._exit(0)
    with open(os.environ["SAID_DONE_TEST_CHILD_PID"], "w", encoding="utf-8") as handle:
        handle.write(str(pid))
    time.sleep(30)
    os._exit(0)
if mode == "stderr":
    sys.stderr.write("secret-token-must-not-leak\\n")
    sys.stderr.flush()
    sys.exit(3)
if mode == "flood":
    sys.stdout.write("x" * (2 * 1024 * 1024))
    sys.stdout.flush()
    sys.exit(0)
if mode == "symlink":
    os.symlink(os.environ["SAID_DONE_TEST_LINK_TARGET"], args[args.index("-o") + 1])
    sys.exit(0)
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
mode = os.environ.get("SAID_DONE_TEST_MODE", "ok")
if mode == "error":
    sys.stdout.write(json.dumps({"is_error": True, "result": "boom"}))
    sys.exit(0)
if mode == "stderr":
    sys.stderr.write("secret-token-must-not-leak\\n")
    sys.exit(4)
sys.stdout.write(json.dumps({"result": json.dumps({"decisions": []}), "is_error": False}))
'''


def make_cli(tmp_path, source, name='fake-cli'):
    path = tmp_path / name
    path.write_text(source.replace('#!/usr/bin/env python3', f'#!{sys.executable}'), encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def recorded(tmp_path, monkeypatch):
    record = tmp_path / 'argv.json'
    monkeypatch.setenv('SAID_DONE_TEST_RECORD', str(record))
    return record


def pid_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------- risk gate ----
@pytest.mark.parametrize('value', [False, None, 1, 'yes', 'true'])
def test_acknowledgement_must_be_true_not_merely_truthy(value):
    with pytest.raises(SubscriptionRiskNotAcknowledged) as error:
        CodexSubscriptionProvider(acknowledge_subscription_risk=value)
    assert error.value.retryable is False
    with pytest.raises(SubscriptionRiskNotAcknowledged):
        ClaudeSubscriptionProvider(acknowledge_subscription_risk=value)


def test_acknowledgement_error_carries_the_disclaimer():
    with pytest.raises(SubscriptionRiskNotAcknowledged) as error:
        ClaudeSubscriptionProvider()
    text = str(error.value)
    assert '一切后果由你自行承担' in text
    assert 'All consequences are your own responsibility.' in text


def test_acknowledged_providers_are_built():
    assert CodexSubscriptionProvider(acknowledge_subscription_risk=True).kind == 'codex-subscription'
    assert ClaudeSubscriptionProvider(acknowledge_subscription_risk=True).kind == 'claude-subscription'


# ------------------------------------------------------------ codex CLI -----
def test_codex_argv_omits_the_model_flag_for_the_default_alias(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = recorded(tmp_path, monkeypatch)
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    answer = provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                                json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert answer == '{"decisions": []}'
    assert '-m' not in argv
    assert argv[0] == 'exec'
    assert '--sandbox' in argv and argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '--skip-git-repo-check' in argv
    assert '--ephemeral' in argv
    # strict structured outputs reject the gate schema (HTTP 400), so it is opt-in
    assert '--output-schema' not in argv
    assert '-o' in argv
    assert argv[-1] == '-'


def test_codex_output_schema_is_opt_in(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = recorded(tmp_path, monkeypatch)
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli),
                                         use_output_schema=True)
    provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                      json_schema={'type': 'object'}, max_output_tokens=100)
    assert '--output-schema' in json.loads(record.read_text())


def test_cli_error_code_extracts_only_the_code():
    from said_done.providers.cli_runner import cli_error_code
    stderr = ('warning line\nERROR: {"error":{"message":"PRIVATE transcript text","type":"invalid_request_error",'
              '"code":"invalid_json_schema"}}')
    assert cli_error_code(stderr) == 'invalid_json_schema'
    assert cli_error_code('ERROR: {"error":{"code":"has spaces and PRIVATE text"}}') is None
    assert cli_error_code('no json here') is None


def test_codex_argv_carries_model_and_effort(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = recorded(tmp_path, monkeypatch)
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-6-luna', effort='low',
                       json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert argv[argv.index('-m') + 1] == 'gpt-6-luna'
    assert argv[argv.index('-c') + 1] == 'model_reasoning_effort="low"'


def test_codex_omits_the_effort_flag_for_none(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    record = recorded(tmp_path, monkeypatch)
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model='gpt-6-luna', effort='none',
                       json_schema=None, max_output_tokens=100)
    assert '-c' not in json.loads(record.read_text())


def test_codex_runs_in_an_empty_directory(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    marker = tmp_path / 'cwd-marker'
    monkeypatch.setenv('SAID_DONE_TEST_RECORD', str(tmp_path / 'argv.json'))
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                       json_schema=None, max_output_tokens=100)
    assert not marker.exists()


def test_codex_timeout_kills_the_process_group(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    pid_file = tmp_path / 'child.pid'
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'hang')
    monkeypatch.setenv('SAID_DONE_TEST_CHILD_PID', str(pid_file))
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli),
                                         timeout=1.0)
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert error.value.retryable is True
    assert 'timed out' in str(error.value)
    assert pid_file.exists()
    assert pid_gone(int(pid_file.read_text().strip())) is True


def test_codex_stderr_is_never_echoed(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'stderr')
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert 'secret-token-must-not-leak' not in str(error.value)
    assert 'exited with code 3' in str(error.value)


def test_codex_output_over_the_limit_is_refused(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'flood')
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert error.value.retryable is True
    assert 'output' in str(error.value).lower()


def test_codex_answer_file_must_be_a_regular_file(tmp_path, monkeypatch):
    target = tmp_path / 'target.txt'
    target.write_text('payload', encoding='utf-8')
    cli = make_cli(tmp_path, CODEX_FAKE, 'codex')
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'symlink')
    monkeypatch.setenv('SAID_DONE_TEST_LINK_TARGET', str(target))
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert 'no answer' in str(error.value)


def test_bounded_answer_read_rejects_oversized_and_irregular_files(tmp_path):
    big = tmp_path / 'big.txt'
    big.write_text('x' * (MAX_OUTPUT + 10), encoding='utf-8')
    with pytest.raises(ProviderError) as error:
        read_bounded_answer(big)
    assert error.value.error_type == 'OutputLimit'
    link = tmp_path / 'link.txt'
    os.symlink(big, link)
    assert read_bounded_answer(link) == ''
    assert read_bounded_answer(tmp_path / 'missing.txt') == ''


def test_codex_missing_executable_is_permanent(tmp_path):
    provider = CodexSubscriptionProvider(acknowledge_subscription_risk=True,
                                         executable=str(tmp_path / 'not-here'))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert error.value.retryable is False
    assert error.value.error_type == 'ExecutableNotFound'


# ---------------------------------------------------------- claude CLI -----
def test_claude_argv_disables_every_tool_and_inherited_mcp_servers(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CLAUDE_FAKE, 'claude')
    record = recorded(tmp_path, monkeypatch)
    provider = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    answer = provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                                json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert answer == '{"decisions": []}'
    assert argv[:3] == ['-p', '--output-format', 'json']
    assert '--model' not in argv
    assert argv[argv.index('--tools') + 1] == ''
    assert '--strict-mcp-config' in argv
    assert argv[argv.index('--mcp-config') + 1] == '{"mcpServers":{}}'


def test_claude_argv_carries_model_and_effort(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CLAUDE_FAKE, 'claude')
    record = recorded(tmp_path, monkeypatch)
    provider = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    provider.complete([{'role': 'user', 'content': 'hi'}], model='sonnet', effort='low',
                       json_schema=None, max_output_tokens=100)
    argv = json.loads(record.read_text())
    assert argv[argv.index('--model') + 1] == 'sonnet'
    assert argv[argv.index('--effort') + 1] == 'low'


def test_claude_error_result_is_permanent(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CLAUDE_FAKE, 'claude')
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'error')
    provider = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert error.value.retryable is False
    assert 'boom' not in str(error.value)


def test_claude_stderr_is_never_echoed(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, CLAUDE_FAKE, 'claude')
    monkeypatch.setenv('SAID_DONE_TEST_MODE', 'stderr')
    provider = ClaudeSubscriptionProvider(acknowledge_subscription_risk=True, executable=str(cli))
    with pytest.raises(ProviderError) as error:
        provider.complete([{'role': 'user', 'content': 'hi'}], model='default', effort=None,
                           json_schema=None, max_output_tokens=100)
    assert 'secret-token-must-not-leak' not in str(error.value)
    assert 'exited with code 4' in str(error.value)


def test_subscription_model_lists_are_builtin():
    codex = {m.id for m in CodexSubscriptionProvider(acknowledge_subscription_risk=True).list_models()}
    claude = {m.id for m in ClaudeSubscriptionProvider(acknowledge_subscription_risk=True).list_models()}
    assert 'default' in codex and 'gpt-6-luna' in codex
    assert {'default', 'opus', 'sonnet', 'haiku'} <= claude
    assert 'claude-haiku-5-5' in claude


def test_bounded_answer_refuses_directory_and_fifo_without_blocking(tmp_path):
    assert read_bounded_answer(tmp_path) == ''
    if hasattr(os, 'mkfifo'):
        fifo = tmp_path / 'answer.fifo'
        os.mkfifo(fifo)
        assert read_bounded_answer(fifo) == ''


def test_bounded_answer_limits_bytes_not_unicode_characters(tmp_path):
    answer = tmp_path / 'answer.json'
    answer.write_text('界' * 10, encoding='utf-8')
    with pytest.raises(ProviderError) as error:
        read_bounded_answer(answer, limit=20)
    assert error.value.error_type == 'OutputLimit'
