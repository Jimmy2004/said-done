"""Executor package: preflight, workspace handling and the Codex adapters.

The public surface is intentionally small so the runtime factory can build
every component from explicit configuration.
"""
from .artifact_validator import validate_artifacts, validate_file
from .cli_adapter import CodexCliError, CodexCliExecutor
from .codex_adapter import (
    AppServerClient,
    CodexAppServerError,
    CodexAppServerExecutor,
    CodexAppServerTimeout,
    build_readonly_probe_script,
)
from .codex_binary import CodexBinaryNotFound, codex_version, resolve_codex_executable
from .config import (
    AppServerConfig,
    ArtifactValidatorConfig,
    CliExecutorConfig,
    ExecutionConfig,
    MockExecutorConfig,
    PreflightConfig,
)
from .mock_executor import MockExecutor
from .preflight import (
    check_dependencies,
    check_goal,
    check_workspace,
    resolve_inputs,
    resolve_outputs,
    run_preflight,
)
from .prompt import build_execution_prompt, build_preflight_prompt
from .workspace import (
    WorkspacePlan,
    backup_file,
    collect_artifacts,
    output_paths_for,
    prepare_workspace,
)

__all__ = [
    "AppServerClient",
    "AppServerConfig",
    "ArtifactValidatorConfig",
    "CliExecutorConfig",
    "CodexAppServerError",
    "CodexAppServerExecutor",
    "CodexAppServerTimeout",
    "CodexBinaryNotFound",
    "CodexCliError",
    "CodexCliExecutor",
    "ExecutionConfig",
    "MockExecutor",
    "MockExecutorConfig",
    "PreflightConfig",
    "WorkspacePlan",
    "backup_file",
    "build_execution_prompt",
    "build_readonly_probe_script",
    "check_dependencies",
    "check_goal",
    "check_workspace",
    "codex_version",
    "collect_artifacts",
    "output_paths_for",
    "prepare_workspace",
    "resolve_codex_executable",
    "resolve_inputs",
    "resolve_outputs",
    "run_preflight",
    "validate_artifacts",
    "validate_file",
]
