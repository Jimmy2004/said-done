"""Settings v2 (see docs/DESIGN.md §1).

Nested sections are the source of truth. Legacy flat v1 keys are still
accepted as input (config files, ``SAID_DONE_*`` env vars, ``Settings(...)``
kwargs) and are mapped into the nested sections; read-only properties with
the old names keep existing call sites working while they are migrated.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_TRIGGER_KEYWORDS = ['我要', '我需要', '帮我', '请帮', '取消', '撤销', '已经完成', '已经做完']
DEFAULT_CANCEL_KEYWORDS = ['取消', '撤销', 'cancel']
DEFAULT_COMPLETION_KEYWORDS = ['已完成', '已经完成', '已经做完', '做完了', 'completed']
DEFAULT_TASK_TYPES = ['CREATE_DOCUMENT', 'CONVERT_FILE', 'WRITE_CODE', 'MODIFY_FILE']

# openai-compatible = OpenAI Responses API (/responses); chat-completions = /chat/completions
# (MiniMax, DeepSeek, gateways...). 'minimax' is kept only for legacy configs.
ClassifierKind = Literal['openai-compatible', 'chat-completions', 'anthropic', 'minimax',
                         'codex-subscription', 'claude-subscription', 'mock']
ExecutorKind = Literal['mock', 'codex-cli', 'codex-app-server', 'claude-code', 'opencode', 'pi',
                       'dsh', 'custom-command', 'host', 'claude-desktop', 'chatgpt-app']


class Section(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)


class SourceConfig(Section):
    id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    kind: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


class ClassifierConfig(Section):
    kind: ClassifierKind = 'mock'
    profile: str = Field(default='default', pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    base_url: str | None = None
    model: str | None = None
    effort: str | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=5)
    use_response_format: bool = False
    # codex-subscription: 'program' = the model must run a deterministic decision
    # program that validates and writes the answer file; 'final-message' = parse
    # the model's last message as JSON (legacy behaviour).
    cli_response_mode: Literal['program', 'final-message'] = 'program'
    acknowledge_subscription_risk: bool = False


class BatchConfig(Section):
    heartbeat_interval_seconds: int = Field(default=600, gt=0)
    min_content_chars: int = Field(default=200, gt=0)
    trigger_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_TRIGGER_KEYWORDS))
    max_batch_wait_seconds: int = Field(default=1800, gt=0)
    max_batch_segments: int = Field(default=50, gt=0)


class GateConfig(Section):
    allowed_task_types: list[str] = Field(default_factory=lambda: list(DEFAULT_TASK_TYPES))
    cancel_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_CANCEL_KEYWORDS))
    completion_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_COMPLETION_KEYWORDS))
    default_document_format: str = 'docx'
    default_code_format: str = 'py'
    extra_instructions: str = Field(default='', max_length=4000)
    # Hard cap: each attempt can itself make classifier.max_attempts model calls.
    max_classification_attempts: int = Field(default=3, ge=1, le=10)
    retry_delay_seconds: int = Field(default=30, ge=0)


class ScreeningConfig(Section):
    screening_only: bool = False
    batch: BatchConfig = Field(default_factory=BatchConfig)
    gate: GateConfig = Field(default_factory=GateConfig)


class PreflightSettings(Section):
    max_scan_files: int = Field(default=5000, gt=0)
    max_scan_depth: int = Field(default=4, ge=0)
    max_scan_seconds: float = Field(default=10, gt=0)


class ExecutorSettings(Section):
    kind: ExecutorKind = 'mock'
    executable: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_seconds: float = Field(default=900, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)
    allowed_input_roots: list[Path] = Field(default_factory=list)
    preflight: PreflightSettings = Field(default_factory=PreflightSettings)


# legacy flat key -> (section path, field)
_LEGACY: dict[str, tuple[tuple[str, ...], str]] = {
    'screening_only': (('screening',), 'screening_only'),
    'heartbeat_interval': (('screening', 'batch'), 'heartbeat_interval_seconds'),
    'min_content_chars': (('screening', 'batch'), 'min_content_chars'),
    'max_batch_wait': (('screening', 'batch'), 'max_batch_wait_seconds'),
    'max_batch_segments': (('screening', 'batch'), 'max_batch_segments'),
    'max_classification_attempts': (('screening', 'gate'), 'max_classification_attempts'),
    'retry_delay': (('screening', 'gate'), 'retry_delay_seconds'),
    'allowed_input_roots': (('executor',), 'allowed_input_roots'),
    'preflight_max_scan_files': (('executor', 'preflight'), 'max_scan_files'),
    'preflight_max_scan_depth': (('executor', 'preflight'), 'max_scan_depth'),
    'preflight_max_scan_seconds': (('executor', 'preflight'), 'max_scan_seconds'),
    'execution_timeout_seconds': (('executor',), 'timeout_seconds'),
    'codex_executable': (('executor',), 'executable'),
    'codex_model': (('executor',), 'model'),
    'minimax_model': (('classifier',), 'model'),
    'minimax_base_url': (('classifier',), 'base_url'),
    'minimax_response_format': (('classifier',), 'use_response_format'),
}
_LEGACY_SPECIAL = {'semantic_mode', 'executor_mode'}
_LEGACY_EXECUTOR = {'mock': 'mock', 'app-server': 'codex-app-server', 'cli': 'codex-cli'}


def _migrate_legacy(values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    if not any(k in values for k in (*_LEGACY, *_LEGACY_SPECIAL)):
        return values

    def section(path: tuple[str, ...]) -> dict[str, Any]:
        node = values
        for name in path:
            current = node.get(name)
            if isinstance(current, BaseModel):
                current = current.model_dump()
            node[name] = dict(current or {})
            node = node[name]
        return node

    for key, (path, field) in _LEGACY.items():
        if key in values:
            section(path)[field] = values.pop(key)
    if 'semantic_mode' in values:
        mode = values.pop('semantic_mode')
        section(('classifier',))['kind'] = 'minimax' if mode == 'minimax' else 'mock'
    if 'executor_mode' in values:
        mode = values.pop('executor_mode')
        section(('executor',))['kind'] = _LEGACY_EXECUTOR.get(mode, mode)
    return values


def config_home() -> Path:
    raw = os.getenv('SAID_DONE_HOME')
    return Path(raw).expanduser() if raw else Path.home() / '.config' / 'said-done'


def default_config_path() -> Path:
    return config_home() / 'config.json'


def resolve_config_path(explicit: Path | None = None) -> Path:
    """One rule for CLI, worker and MCP: --config, else $SAID_DONE_CONFIG, else the default path."""
    raw = explicit or os.getenv('SAID_DONE_CONFIG')
    return Path(raw).expanduser() if raw else default_config_path()


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)

    schema_version: Literal[2] = 2
    data_dir: Path = Field(default_factory=lambda: config_home() / 'data')
    sources: list[SourceConfig] = Field(default_factory=lambda: [SourceConfig(id='manual', kind='manual')])
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    executor: ExecutorSettings = Field(default_factory=ExecutorSettings)
    run_scheduler_in_server: bool = False

    @model_validator(mode='before')
    @classmethod
    def _accept_legacy(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values = _migrate_legacy(values)
            values.setdefault('schema_version', 2)
        return values

    @field_validator('data_dir')
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode='after')
    def _unique_sources(self):
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError('source ids must be unique')
        return self

    # ---- read-only legacy views (migrate call sites to nested fields) ----
    @property
    def semantic_mode(self) -> str:
        return self.classifier.kind

    @property
    def executor_mode(self) -> str:
        return {'codex-app-server': 'app-server', 'codex-cli': 'cli'}.get(self.executor.kind, self.executor.kind)

    screening_only = property(lambda self: self.screening.screening_only)
    heartbeat_interval = property(lambda self: self.screening.batch.heartbeat_interval_seconds)
    min_content_chars = property(lambda self: self.screening.batch.min_content_chars)
    max_batch_wait = property(lambda self: self.screening.batch.max_batch_wait_seconds)
    max_batch_segments = property(lambda self: self.screening.batch.max_batch_segments)
    max_classification_attempts = property(lambda self: self.screening.gate.max_classification_attempts)
    retry_delay = property(lambda self: self.screening.gate.retry_delay_seconds)
    allowed_input_roots = property(lambda self: self.executor.allowed_input_roots)
    preflight_max_scan_files = property(lambda self: self.executor.preflight.max_scan_files)
    preflight_max_scan_depth = property(lambda self: self.executor.preflight.max_scan_depth)
    preflight_max_scan_seconds = property(lambda self: self.executor.preflight.max_scan_seconds)
    execution_timeout_seconds = property(lambda self: self.executor.timeout_seconds)
    codex_executable = property(lambda self: self.executor.executable or 'codex')
    codex_model = property(lambda self: self.executor.model)
    minimax_model = property(lambda self: self.classifier.model or 'MiniMax-M3.1-Flash')
    minimax_base_url = property(lambda self: self.classifier.base_url or 'https://api.minimaxi.com')
    minimax_response_format = property(lambda self: self.classifier.use_response_format)


_ENV_JSON_KEYS = {'allowed_input_roots', 'sources', 'classifier', 'screening', 'executor'}


def load_settings(path: Path | None = None) -> Settings:
    """Load config: explicit path, else nothing (defaults). Env ``SAID_DONE_*`` overrides."""
    load_dotenv((path.resolve().parent if path else Path.cwd()) / '.env', override=False)
    values: dict[str, Any] = json.loads(path.read_text('utf-8')) if path and path.exists() else {}
    names = set(Settings.model_fields) | set(_LEGACY) | _LEGACY_SPECIAL
    for key in names:
        value = os.getenv('SAID_DONE_' + key.upper())
        if value is not None:
            values[key] = json.loads(value) if key in _ENV_JSON_KEYS else value
    return Settings.model_validate(values)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Atomically write config (0600). Never contains secrets by construction."""
    path = Path(path or default_config_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = settings.model_dump(mode='json')
    fd, tmp = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON merge-patch."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def patch_section(settings: Settings, section: str, patch: dict[str, Any]) -> Settings:
    """Return a validated copy of settings with one top-level section merge-patched."""
    if section not in Settings.model_fields or section == 'schema_version':
        raise ValueError(f'unknown settings section {section!r}')
    data = settings.model_dump(mode='json')
    data[section] = merge_patch(data[section], patch)
    return Settings.model_validate(data)
