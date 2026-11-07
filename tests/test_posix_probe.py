import os
import subprocess
from types import SimpleNamespace

import pytest

from said_done.executor import codex_adapter as adapter
from said_done.executor.config import AppServerConfig


@pytest.mark.parametrize('platform', ['posix', 'nt'])
@pytest.mark.parametrize('shell', [None, '/custom/shell'])
def test_probe_argv(tmp_path, monkeypatch, platform, shell):
    paths = [tmp_path / "a ' $(touch injected); file"]
    executor = adapter.CodexAppServerExecutor(config=AppServerConfig(probe_shell=shell))
    calls = []
    def request(method, params, **kwargs):
        calls.append((method, params))
        return {'stdout': 'OK|none\n', 'exitCode': 0}
    with monkeypatch.context() as patch:
        patch.setattr(os, 'name', platform)
        assert executor._probe_readonly(SimpleNamespace(request=request), tmp_path, paths) == (True, [])
    method, params = calls[0]
    assert method == 'command/exec'
    if platform == 'nt':
        assert params['command'] == [shell or 'powershell.exe', '-NoProfile', '-NonInteractive',
                                     '-Command', adapter.build_readonly_probe_script(paths)]
    else:
        assert params['command'] == [shell or '/bin/sh', '-c', adapter.POSIX_PROBE_SCRIPT,
                                     'said-done-probe', str(paths[0])]
        assert str(paths[0]) not in adapter.POSIX_PROBE_SCRIPT
    assert params['sandboxPolicy'] == {'type': 'readOnly', 'networkAccess': False}


@pytest.mark.skipif(os.name == 'nt', reason='requires /bin/sh')
def test_real_posix_probe(tmp_path):
    existing = tmp_path / "a ' $(touch injected); file"
    existing.write_text('data')
    missing = tmp_path / 'missing'
    unreadable = tmp_path / 'unreadable'
    unreadable.write_text('secret')
    unreadable.chmod(0)
    paths = [existing, missing, tmp_path]
    if os.geteuid() != 0:
        paths.append(unreadable)
    try:
        result = subprocess.run(['/bin/sh', '-c', adapter.POSIX_PROBE_SCRIPT, 'said-done-probe',
                                 *map(str, paths)], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0
        assert result.stdout.splitlines() == [f'OK|{existing}'] + [
            f'FAIL|{p}|missing, not a file, or unreadable' for p in paths[1:]]
        empty = subprocess.run(['/bin/sh', '-c', adapter.POSIX_PROBE_SCRIPT, 'said-done-probe'],
                               capture_output=True, text=True, timeout=5, check=True)
        assert empty.stdout == 'OK|none\n'
        assert not (tmp_path / 'injected').exists()
    finally:
        unreadable.chmod(0o600)
