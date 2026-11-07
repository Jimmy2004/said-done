"""Plain service functions; external providers are imported only when used."""
from __future__ import annotations

import os
from importlib.resources import files
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from said_done.config import Settings, ScreeningConfig, default_config_path, patch_section, save_settings


def config_path() -> Path:
    from said_done.config import resolve_config_path
    return resolve_config_path()


def get_setup_guide() -> dict:
    """Read the same guide from a checkout or an installed distribution."""
    checkout = Path(__file__).resolve().parents[2] / 'docs' / 'AGENT_SETUP.md'
    if checkout.is_file():
        return {'guide': checkout.read_text('utf-8')}
    return {'guide': files('said_done').joinpath('docs/AGENT_SETUP.md').read_text('utf-8')}



def _save(settings: Settings, section: str, patch: Any, path: Path | None) -> Settings:
    # Keys must enter through the local credential importer, never configuration.
    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in ('api_key', 'apikey', 'password', 'secret', 'authorization', 'access_token', 'token'):
                    raise ValueError('credentials must use the credential importer')
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(patch)
    updated = patch_section(settings, section, patch)
    save_settings(updated, path or config_path())
    return updated


def setup_begin(settings: Settings, profile: str = 'default', *, path: Path | None = None) -> dict:
    """Create a credentials template and enable screening-only for a new setup."""
    from said_done.credentials import begin_import
    if not (path or config_path()).exists():
        settings = _save(settings, 'screening', {'screening_only': True}, path)
    return begin_import(settings, profile)


def import_credentials(settings: Settings, profile: str = 'default', *, path: Path | None = None) -> dict:
    from said_done.credentials import import_credentials as import_local
    result = dict(import_local(settings, profile))
    patch = result.pop('updated_classifier', None)
    if patch:
        _save(settings, 'classifier', {**patch, 'profile': profile}, path)
    return result


def credential_status(settings: Settings, profile: str = 'default') -> dict:
    from said_done.credentials import credential_status as inspect_credentials
    return inspect_credentials(settings, profile)


def delete_credentials(settings: Settings, profile: str = 'default') -> dict:
    from said_done.credentials import delete_credentials as delete
    return delete(settings, profile)


def probe_api(settings: Settings, profile: str = 'default') -> dict:
    """Probe the configured endpoint without sending a model or spending tokens."""
    from said_done.credentials import get_api_key
    from said_done.credentials.paths import validate_profile
    from said_done.providers.probe import probe_api as probe
    profile = validate_profile(profile)
    key = get_api_key(settings, profile)
    result = probe(settings.classifier.base_url, key.get_secret_value() if key else '')
    # Scrub this profile as well as the active runtime profile.
    def scrub(value):
        if isinstance(value, str):
            return value.replace(key.get_secret_value(), '[REDACTED]') if key else value
        if isinstance(value, dict):
            return {scrub(k): scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return public_result(settings, scrub(result))


def list_models(settings: Settings) -> dict:
    from said_done.providers import list_models as models
    return {'models': models(settings)}


def configure_classifier(settings: Settings, patch: dict, *, path: Path | None = None) -> dict:
    candidate = patch_section(settings, 'classifier', patch)
    subscription = candidate.classifier.kind in ('codex-subscription', 'claude-subscription')
    if subscription:
        from said_done.providers import SUBSCRIPTION_DISCLAIMER
    if subscription and patch.get('acknowledge_subscription_risk') is not True:
        return {'configured': False, 'requires_acknowledgement': True, 'disclaimer': SUBSCRIPTION_DISCLAIMER}
    updated = _save(settings, 'classifier', patch, path)
    result = {'configured': True, 'classifier': updated.classifier.model_dump(mode='json')}
    if subscription:
        result['disclaimer'] = SUBSCRIPTION_DISCLAIMER
    return result


def test_classifier(settings: Settings) -> dict:
    """Classify built-in synthetic input, never a user's pending transcript."""
    from said_done.semantic.factory import test_classifier as test
    return test(settings)


def explain_screening(settings: Settings) -> dict:
    from said_done.semantic.screening import explain_screening as explain
    return explain(settings)


def _screening_result(settings: Settings) -> dict:
    values = settings.screening.model_dump(mode='json')
    defaults = ScreeningConfig().model_dump(mode='json')
    defaults['screening_only'] = True
    def diff(actual: dict, default: dict) -> dict:
        return {key: diff(value, default.get(key, {})) if isinstance(value, dict) else value
                for key, value in actual.items() if value != default.get(key)}
    return {'values': values, 'defaults': defaults, 'differences': diff(values, defaults)}


def configure_screening(settings: Settings, patch: dict, *, path: Path | None = None) -> dict:
    return _screening_result(_save(settings, 'screening', patch, path))


def reset_screening_defaults(settings: Settings, *, path: Path | None = None) -> dict:
    defaults = ScreeningConfig().model_dump(mode='json')
    defaults['screening_only'] = True
    return configure_screening(settings, defaults, path=path)


def list_sources(settings: Settings) -> dict:
    from said_done.ingestion.registry import list_source_kinds
    return {'sources': list_source_kinds(), 'configured': [s.model_dump(mode='json') for s in settings.sources]}


def configure_sources(settings: Settings, sources: list[dict], *, path: Path | None = None) -> dict:
    from said_done.ingestion.registry import list_source_kinds
    available = {s['kind'] for s in list_source_kinds()}
    if any(s.get('kind') not in available for s in sources):
        raise ValueError('source kind is not installed')
    updated = _save(settings, 'sources', sources, path)
    return {'sources': [s.model_dump(mode='json') for s in updated.sources]}


def list_executors(settings: Settings) -> dict:
    from said_done.executor.registry import list_executors as executors
    return {'executors': executors()}


def configure_executor(settings: Settings, patch: dict, *, path: Path | None = None) -> dict:
    from said_done.executor.registry import executor_info
    candidate = patch_section(settings, 'executor', patch)
    info = executor_info(candidate.executor.kind)
    updated = _save(settings, 'executor', patch, path)
    warnings = list(info.get('warnings', info.get('notes', [])))
    extra = {}
    if candidate.executor.kind == 'opencode' and not candidate.executor.model:
        warnings.append('OpenCode free default may hit usage limits; ask the user to choose executor.model from a connected provider')
        extra['models'] = info.get('models', [])
    return {**extra, 'executor': updated.executor.model_dump(mode='json'), 'info': info,
            'needs_user_verification': info.get('verification') == 'experimental-unverified',
            'sandbox': info.get('sandbox'), 'warnings': warnings}


def test_executor(settings: Settings) -> dict:
    """Detect and run local preflight only; never invoke an executor CLI."""
    from said_done.executor.registry import executor_info
    from said_done.executor.preflight import run_preflight
    from said_done.executor.config import PreflightConfig
    from said_done.contracts import TaskPacket
    task = TaskPacket(task_type='WRITE_CODE', goal='Synthetic preflight check',
                      evidence_segment_ids=['synthetic'], original_evidence=['synthetic'],
                      expected_outputs=['check.py'], requirements=['Write a Python file containing a hello-world print.'],
                      dedup_key='synthetic')
    config = settings.executor.preflight
    with tempfile.TemporaryDirectory(prefix='said-done-preflight-') as directory:
        workspace = Path(directory)
        (workspace / 'output').mkdir()
        result = run_preflight(task, workspace, PreflightConfig(
            readonly_roots=tuple(settings.executor.allowed_input_roots), max_scan_files=config.max_scan_files,
            max_scan_depth=config.max_scan_depth, max_scan_seconds=config.max_scan_seconds))
    return {'dry_run': True, 'info': executor_info(settings.executor.kind),
            'preflight': result.model_dump(mode='json')}


def _repo(settings: Settings):
    from said_done.storage.database import Database
    from said_done.storage.repositories import Repository
    return Repository(Database(settings.data_dir.expanduser() / 'core.db'))


def submit_transcript(settings: Settings, text: str, segment_id: str | None = None,
                      started_at: str | None = None, ended_at: str | None = None,
                      source: str = 'manual') -> dict:
    from said_done.contracts import TranscriptSegment, utcnow
    if not any(s.id == source and s.kind == 'manual' for s in settings.sources):
        raise ValueError('select a configured manual source')
    now = utcnow()
    segment = TranscriptSegment(segment_id=segment_id or str(uuid4()), text=text, source=source,
                                timestamp_start=started_at or now, timestamp_end=ended_at or started_at or now)
    return {'inserted': _repo(settings).ingest([segment]), 'segment_id': segment.segment_id, 'source': source}


def process_now(settings: Settings) -> dict:
    from said_done.runtime import create_runtime
    runtime = None
    try:
        runtime = create_runtime(settings.model_copy(update={'data_dir': settings.data_dir.expanduser()}))
        return {'busy': False, 'result': runtime.tick(force=True)}
    except BlockingIOError:
        return {'busy': True}
    except RuntimeError as exc:
        if str(exc) == 'another worker is active':
            return {'busy': True}
        raise
    finally:
        if runtime is not None:
            runtime.close()


def status(settings: Settings) -> dict:
    return _repo(settings).status()


def list_tasks(settings: Settings, state: str | None = None, limit: int = 50) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    return {'tasks': _repo(settings).list_tasks(state=state, limit=limit)}


def task_result(settings: Settings, task_id: str) -> dict:
    result = _repo(settings).task_result(task_id)
    return {'result': result.model_dump(mode='json') if result is not None else None}


def cancel_task(settings: Settings, task_id: str) -> dict:
    return _repo(settings).cancel_task(task_id)


def audit_summary(settings: Settings, since: float | None = None, until: float | None = None) -> dict:
    return _repo(settings).audit_summary(since=since, until=until)


def audit_records(settings: Settings, limit: int = 100, outcome: str | None = None,
                  action: str | None = None, batch_id: str | None = None, task_id: str | None = None,
                  since: float | None = None, until: float | None = None, include_evidence: bool = False) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    records = _repo(settings).audit_records(limit=limit, outcome=outcome, action=action,
                                          batch_id=batch_id, task_id=task_id, since=since, until=until)
    if not include_evidence:
        # Allowlist prevents future repository fields from silently exposing text.
        fields = {'id', 'batch_id', 'task_id', 'outcome', 'action', 'reason', 'model',
                  'attempt', 'created', 'created_iso', 'linked_task_ids'}
        records = [{k: v for k, v in row.items() if k in fields} for row in records]
    return {'records': records}


def retry_classification(settings: Settings) -> dict:
    return {'requeued': _repo(settings).requeue_classification()}


def source_status(settings: Settings) -> dict:
    from said_done.ingestion.registry import build_sources
    sources = build_sources(settings, _repo(settings))
    try:
        return {'sources': [s.status() for s in sources]}
    finally:
        for source in sources:
            source.close()


def claim_next_task(settings: Settings, lease_seconds: int = 7200) -> dict:
    from said_done.broker.host_queue import claim_next_task as claim
    if lease_seconds <= 0:
        raise ValueError('lease_seconds must be positive')
    return {'task': claim(_repo(settings), lease_seconds=lease_seconds)}


def complete_task(settings: Settings, task_id: str, summary: str) -> dict:
    from said_done.broker.host_queue import complete_task as complete
    return complete(_repo(settings), task_id, summary, validate=True)


def release_task(settings: Settings, task_id: str) -> dict:
    from said_done.broker.host_queue import release_task as release
    return release(_repo(settings), task_id)


def setup_status(settings: Settings, *, path: Path | None = None) -> dict:
    """Report observable configuration milestones; do not claim tests or daemon installation."""
    saved = (path or config_path()).exists()
    kind = settings.classifier.kind
    credentials = credential_status(settings, settings.classifier.profile)
    credential_ready = kind in ('mock', 'codex-subscription', 'claude-subscription') or bool(
        credentials.get('exists', credentials.get('stored', False)))
    steps = [
        {'step': 'classifier', 'complete': saved and credential_ready and (kind in ('mock', 'codex-subscription', 'claude-subscription') or bool(settings.classifier.model))},
        {'step': 'screening', 'complete': saved},
        {'step': 'sources', 'complete': saved and bool(settings.sources)},
        {'step': 'executor', 'complete': saved and bool(settings.executor.kind)},
    ]
    next_step = next((s['step'] for s in steps if not s['complete']),
                     'review_screening' if settings.screening.screening_only else 'run_daemon')
    return {'steps': steps, 'next_step': next_step, 'screening_only': settings.screening.screening_only,
            'credentials': credentials, 'tests_verified': False,
            'daemon': {'verified': False, 'command': 'said-done run',
                       'guide': 'guide://setup', 'note': 'MCP does not poll by default; run a separate daemon.'}}


def public_result(settings: Settings, result: dict) -> dict:
    """Defense in depth: scrub known runtime credentials from otherwise public data."""
    from said_done.credentials import get_api_key
    secrets = [v for k, v in os.environ.items() if k.startswith('SAID_DONE_API_KEY_') or k == 'MINIMAX_API_KEY']
    try:
        key = get_api_key(settings, settings.classifier.profile)
        if key:
            secrets.append(key.get_secret_value())
    except Exception:
        pass
    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {scrub(str(k)): scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return scrub(result)
