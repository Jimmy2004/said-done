"""Official SDK registration, with configuration and error handling at the boundary."""
from __future__ import annotations

import inspect
import os
from functools import wraps
from typing import Any, get_type_hints

from mcp.types import CallToolResult, TextContent

try:  # SDK 2.x renamed FastMCP; retain compatibility with 1.x.
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

from said_done.config import load_settings, patch_section
from . import services

TOOL_NAMES = (
    'get_setup_guide', 'setup_status', 'setup_begin', 'import_credentials', 'credential_status',
    'delete_credentials', 'probe_api', 'list_models', 'configure_classifier', 'test_classifier',
    'explain_screening', 'configure_screening', 'reset_screening_defaults', 'list_sources',
    'configure_sources', 'list_executors', 'configure_executor', 'test_executor',
    'submit_transcript', 'process_now', 'status', 'list_tasks', 'task_result', 'cancel_task',
    'audit_summary', 'audit_records', 'retry_classification', 'source_status',
    'claim_next_task', 'complete_task', 'release_task',
)


class SafeMCP(FastMCP):
    """SDK argument validation can echo input; sanitize before it reaches transport/logs."""

    async def call_tool(self, *args: Any, **kwargs: Any):
        try:
            return await super().call_tool(*args, **kwargs)
        except Exception:
            return CallToolResult(isError=True, content=[TextContent(
                type='text', text='{"error":"InvalidArguments","message":"Check tool arguments and prerequisites."}')])


_MESSAGE_ERRORS = (ValueError, FileNotFoundError, NotImplementedError, PermissionError)


def _safe_error(exc: Exception, settings, inputs: dict | None = None) -> dict:
    """Actionable but non-leaking error payload.

    Pydantic errors are reduced to field locations and messages (never the
    rejected input, which may be a transcript or a pasted key). Messages of
    the app's own ValueError-family errors are kept, truncated and scrubbed
    of known credentials. Anything else (HTTP clients, subprocesses) only
    reports its type, because its text may quote provider output.
    """
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        fields = [{'loc': '.'.join(str(p) for p in e.get('loc', ())), 'type': e.get('type'), 'msg': e.get('msg')}
                  for e in exc.errors(include_input=False, include_url=False)][:20]
        return {'error': 'ValidationError', 'fields': fields}
    payload = {'error': type(exc).__name__}
    if isinstance(exc, _MESSAGE_ERRORS) and str(exc):
        message = str(exc)
        # Never reflect caller-supplied text (a transcript, a pasted key).
        for value in _strings(inputs or {}):
            if len(value) >= 12:  # short values are enum-like (e.g. 'manual'), not free text
                message = message.replace(value, '[INPUT]')
        payload['message'] = message[:300]
    else:
        payload['message'] = 'Operation failed; check configuration and local prerequisites.'
    if settings is not None:
        try:
            payload = services.public_result(settings, payload)
        except Exception:
            payload['message'] = 'Operation failed.'
    return payload


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _tool(name: str):
    service = getattr(services, name)
    signature = inspect.signature(service)
    hints = get_type_hints(service)
    # Keep real typed input schemas while hiding dependency injection parameters.
    parameters = [p.replace(annotation=hints.get(p.name, p.annotation))
                  for p in signature.parameters.values() if p.name not in ('settings', 'path')]

    @wraps(service)
    def invoke(**kwargs) -> dict:
        try:
            path = services.config_path()
            settings = load_settings(path)
            if not path.exists():
                if not os.getenv("SAID_DONE_SCREENING") and not os.getenv("SAID_DONE_SCREENING_ONLY"):
                    settings = patch_section(settings, "screening", {"screening_only": True})
                if not os.getenv("SAID_DONE_DATA_DIR"):
                    settings.data_dir = path.parent / "data"
            settings.data_dir = settings.data_dir.expanduser()
            inputs = dict(kwargs)
            if 'settings' in signature.parameters:
                inputs['settings'] = settings
            if 'path' in signature.parameters:
                inputs['path'] = path
            result = getattr(services, name)(**inputs)
            return services.public_result(settings, result)
        except BlockingIOError:
            return {'busy': True}
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) == 'another worker is active':
                return {'busy': True}
            return _safe_error(exc, settings if 'settings' in locals() else None, kwargs)

    invoke.__signature__ = signature.replace(parameters=parameters, return_annotation=dict)
    invoke.__annotations__ = {p.name: p.annotation for p in parameters} | {'return': dict}
    return invoke


def create_server():
    """Build without initializing providers, reading credentials, or starting polling."""
    server = SafeMCP('said-done', instructions=(
        'Turn voice transcripts into screened tasks; first call get_setup_guide and follow its security rules.'))
    for name in TOOL_NAMES:
        server.tool(name=name)(_tool(name))

    @server.resource('guide://setup')
    def setup_guide() -> str:
        return services.get_setup_guide()['guide']

    @server.prompt(name='setup')
    def setup() -> str:
        return services.get_setup_guide()['guide']

    return server


def main() -> None:
    create_server().run(transport='stdio')
