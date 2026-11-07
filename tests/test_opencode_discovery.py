"""Offline discovery: only test-owned executables and configuration files."""
import json
import sys

import pytest

from said_done.executor import registry
from said_done.config import Settings
from said_done.mcp_server import services


@pytest.fixture
def opencode(tmp_path, monkeypatch):
    fake = tmp_path / 'opencode'
    fake.write_text('#!' + sys.executable + '\nimport sys\n'
                    'assert sys.stdin.read() == ""\n'
                    'print("1.2.3" if "--version" in sys.argv else '
                    '"deepseek/deepseek-flash\\ngithub-copilot/gpt-6-luna\\n'
                    'minimax-cn-coding-plan/MiniMax-M3.1-Flash\\nopencode/test-free\\n'
                    'not a model\\nprovider/model with spaces\\n")\n')
    fake.chmod(0o700)
    monkeypatch.setattr(registry.shutil, 'which', lambda _: str(fake))
    config = tmp_path / 'opencode.json'
    monkeypatch.setenv('OPENCODE_CONFIG', str(config))
    return fake, config


@pytest.mark.parametrize('content,model,warn', [
    (None, None, True),
    ('invalid', None, True),
    ('[]', None, True),
    ('{"model":123}', None, True),
    ('{"model":"opencode/test-free"}', 'opencode/test-free', True),
    ('{"model":"deepseek/deepseek-flash"}', 'deepseek/deepseek-flash', False),
    ('{/* comment */ "model": "provider/model", // comment\n "url":"https://example.test"}', 'provider/model', False),
])
def test_models_and_default(opencode, content, model, warn):
    _, config = opencode
    if content is not None:
        config.write_text(content)
    info = registry.executor_info('opencode')
    assert info['models'] == ['deepseek/deepseek-flash', 'github-copilot/gpt-6-luna',
                              'minimax-cn-coding-plan/MiniMax-M3.1-Flash', 'opencode/test-free']
    assert info['configured_default_model'] == model
    assert (registry.OPENCODE_FREE_WARNING in info['notes']) is warn


def test_default_home_location(opencode, monkeypatch, tmp_path):
    monkeypatch.delenv('OPENCODE_CONFIG')
    monkeypatch.setenv('HOME', str(tmp_path))
    config = tmp_path / '.config/opencode/opencode.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'model': 'provider/chosen'}))
    assert registry.executor_info('opencode')['configured_default_model'] == 'provider/chosen'


def test_uninstalled_does_not_read_config(opencode, monkeypatch):
    monkeypatch.setattr(registry.shutil, 'which', lambda _: None)
    monkeypatch.setattr(registry, '_opencode_default_model', lambda: pytest.fail('read config'))
    info = registry.executor_info('opencode')
    assert not info['installed'] and 'models' not in info


@pytest.mark.parametrize('mode', ['many', 'overflow', 'error'])
def test_models_bounds(opencode, mode):
    fake, _ = opencode
    body = {'many': 'print("p/m\\n" * 350)', 'overflow': 'print("x" * 300000)',
            'error': 'raise SystemExit(1)'}[mode]
    fake.write_text('#!' + sys.executable + '\n' + body + '\n')
    models = registry._opencode_models(str(fake))
    assert len(models) == (300 if mode == 'many' else 0)


def test_models_timeout_budget(opencode, monkeypatch):
    def communicate(process, **kwargs):
        assert kwargs == {'timeout': 10, 'limit': 262144}
        process.communicate(timeout=2)
        raise TimeoutError
    monkeypatch.setattr(registry, 'communicate_bounded', communicate)
    assert registry._opencode_models(str(opencode[0])) == []


@pytest.mark.parametrize('model', [None, 'provider/chosen'])
def test_configure_warning_and_model_choice(opencode, tmp_path, model):
    settings = Settings(data_dir=tmp_path / 'data')
    result = services.configure_executor(settings, {'kind': 'opencode', 'model': model},
                                         path=tmp_path / 'settings.json')
    assert result['executor']['model'] == model
    warnings = ' '.join(result['warnings'])
    if model is None:
        assert 'free default may hit usage limits' in warnings
        assert result['models'] == result['info']['models']
    else:
        assert 'free default may hit usage limits' not in warnings
