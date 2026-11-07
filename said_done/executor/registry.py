"""Executor discovery and nested-settings composition."""
from __future__ import annotations
import os
import json
from pathlib import Path
import re
import shutil
import subprocess
from .interface import Executor
from .config import PreflightConfig, ExecutionConfig, CliExecutorConfig, AppServerConfig
from .providers.command import CommandExecutor, CommandOptions, HostExecutor
from .process_control import communicate_bounded

KINDS=('codex-cli','codex-app-server','claude-code','opencode','pi','dsh','custom-command',
       'host','claude-desktop','chatgpt-app','mock')
HOST={'host','claude-desktop','chatgpt-app'}


OPENCODE_FREE_WARNING = ('OpenCode default model is a free model with usage limits; '
                         'choose a model from a connected provider via executor.model')


def _opencode_default_model() -> str | None:
    path = Path(os.environ.get('OPENCODE_CONFIG') or '~/.config/opencode/opencode.json').expanduser()
    try:
        # Match strings first so comment markers inside model IDs/URLs survive.
        text = path.read_text(encoding='utf-8')
        text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                      lambda m: m.group() if m.group().startswith('"') else ' ',
                      text, flags=re.S)
        config = json.loads(text)
        model = config.get('model') if isinstance(config, dict) else None
        return model if isinstance(model, str) and model else None
    except (OSError, ValueError):
        return None


def _opencode_models(path: str) -> list[str]:
    try:
        process = subprocess.Popen([path, 'models'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=os.name != 'nt')
        stdout, _ = communicate_bounded(process, timeout=10, limit=262144)
        if process.returncode != 0:
            return []
        return [line.strip() for line in stdout.splitlines()
                if re.fullmatch(r'[\w.-]+/[\w.:/-]+', line.strip())][:300]
    except Exception:
        return []


def executor_info(kind: str) -> dict:
    if kind not in KINDS:
        raise ValueError('unknown executor kind')
    executable={'codex-cli':'codex','codex-app-server':'codex','claude-code':'claude',
                'opencode':'opencode','pi':'pi','dsh':'dsh'}.get(kind)
    path=shutil.which(executable) if executable else None
    version=None
    notes=[]
    if path:
        try:
            process=subprocess.Popen([path,'--version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                     text=True,start_new_session=os.name!='nt')
            stdout,_=communicate_bounded(process,timeout=2,limit=16384)
            match=re.search(r'\b\d+\.\d+(?:\.\d+)?\b',stdout)
            version=match.group() if match and process.returncode==0 else None
        except Exception:
            notes.append('Version probe unavailable')
    unverified=kind in {'pi','dsh','custom-command','claude-desktop','chatgpt-app'}
    sandbox='Codex OS sandbox' if kind.startswith('codex-') else 'host-defined' if kind in HOST else 'none'
    if sandbox=='none' and kind!='mock':
        notes.append('No OS sandbox; workspace and tool permissions do not isolate the operating system')
    if unverified:
        notes.append('Requires local user verification; CLI flags or host integration may need adjustment')
    if kind=='dsh':
        notes.append('Explicit user-confirmed argv template required; tool identity is unspecified')
    extra = {}
    if kind == 'opencode' and path:
        model = _opencode_default_model()
        extra = {'models': _opencode_models(path), 'configured_default_model': model}
        if not model or (model.startswith('opencode/') and model.endswith('-free')):
            notes.append(OPENCODE_FREE_WARNING)
    return {**extra, 'kind':kind,'installed':bool(path) if executable else kind in HOST or kind=='mock',
            'path':path,'version':version,'notes':notes,'install_hint':f'Install {executable} and authenticate it locally' if executable else 'Configure host MCP or an explicit command template',
            'verification':'experimental-unverified' if unverified else 'verified','sandbox':sandbox,
            'needs_user_verification':unverified,'options_schema':CommandOptions.model_json_schema() if kind in {'claude-code','opencode','pi','dsh','custom-command'} else {'type':'object','properties':{},'additionalProperties':False}}


def list_executors() -> list[dict]:
    return [executor_info(kind) for kind in KINDS]


def build_executor(settings) -> Executor:
    e=settings.executor
    p=e.preflight
    common={'preflight_config':PreflightConfig(readonly_roots=tuple(e.allowed_input_roots),
                max_scan_files=p.max_scan_files,max_scan_depth=p.max_scan_depth,max_scan_seconds=p.max_scan_seconds),
            'execution_config':ExecutionConfig(timeout_seconds=e.timeout_seconds)}
    if e.kind=='mock':
        from .mock_executor import MockExecutor
        return MockExecutor(**common)
    if e.kind in HOST:
        executor=HostExecutor(**common)
        executor.kind=e.kind
        if e.kind!='host':
            executor.verification='experimental-unverified'
        return executor
    if e.kind=='codex-cli':
        from .cli_adapter import CodexCliExecutor
        return CodexCliExecutor(config=CliExecutorConfig(executable=e.executable,model=e.model,effort=e.effort,
                                                       timeout_seconds=e.timeout_seconds),**common)
    if e.kind=='codex-app-server':
        from .codex_adapter import CodexAppServerExecutor
        return CodexAppServerExecutor(config=AppServerConfig(executable=e.executable,model=e.model,effort=e.effort,
                                                           turn_timeout_seconds=e.timeout_seconds),**common)
    from .providers.claude_code import ClaudeCodeExecutor
    from .providers.opencode import OpenCodeExecutor
    from .providers.pi import PiExecutor
    from .providers.dsh import DshExecutor
    from .providers.custom_command import CustomCommandExecutor
    providers={'claude-code':ClaudeCodeExecutor,'opencode':OpenCodeExecutor,'pi':PiExecutor,
               'dsh':DshExecutor,'custom-command':CustomCommandExecutor}
    return providers[e.kind](executable=e.executable,model=e.model,effort=e.effort,options=e.options,**common)
