"""Explicit, bounded configuration for the executor package.

Every knob that can make an executor spend unbounded time, disk or network
lives here so the CLI can build one object per component.  Nothing in this
module reads the environment implicitly: the caller decides the values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PreflightConfig:
    """Budgets and rules for the local, read-only preflight pass.

    ``readonly_roots`` are the only places outside the execution workspace
    where an input file may be found.  An empty tuple means "no external
    roots": inputs must already live inside the workspace (fail closed).
    """

    readonly_roots: tuple[Path, ...] = ()
    max_scan_files: int = 5000
    max_scan_depth: int = 4
    max_scan_seconds: float = 10.0
    max_tool_calls: int = 0
    allow_tool_calls: bool = False
    required_tools: tuple[str, ...] = ()
    required_modules: tuple[str, ...] = ()
    min_goal_chars: int = 8
    output_dir_name: str = "output"

    def __post_init__(self) -> None:
        if self.max_scan_files <= 0:
            raise ValueError("max_scan_files must be > 0")
        if self.max_scan_depth < 0:
            raise ValueError("max_scan_depth must be >= 0")
        if self.max_scan_seconds <= 0:
            raise ValueError("max_scan_seconds must be > 0")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        if self.min_goal_chars < 1:
            raise ValueError("min_goal_chars must be >= 1")
        if not self.output_dir_name or "/" in self.output_dir_name or "\\" in self.output_dir_name:
            raise ValueError("output_dir_name must be a single path segment")


@dataclass(frozen=True)
class ExecutionConfig:
    """Where work happens and how long it may take."""

    timeout_seconds: float = 900.0
    input_dir_name: str = "inputs"
    output_dir_name: str = "output"
    backup_dir_name: str = ".backups"
    min_artifact_bytes: int = 16

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0 (timeouts are mandatory)")
        for name in (self.input_dir_name, self.output_dir_name, self.backup_dir_name):
            if not name or "/" in name or "\\" in name:
                raise ValueError(f"directory name {name!r} must be a single path segment")
        if self.min_artifact_bytes < 0:
            raise ValueError("min_artifact_bytes must be >= 0")


@dataclass(frozen=True)
class ArtifactValidatorConfig:
    """Thresholds used when checking produced files."""

    min_bytes: int = 16
    min_text_chars: int = 60
    min_overlap: float = 0.2
    output_dir_name: str = "output"
    input_dir_name: str = "inputs"

    def __post_init__(self) -> None:
        if self.min_bytes < 0:
            raise ValueError("min_bytes must be >= 0")
        if self.min_text_chars < 0:
            raise ValueError("min_text_chars must be >= 0")
        if not 0.0 <= self.min_overlap <= 1.0:
            raise ValueError("min_overlap must be between 0 and 1")
        if not self.output_dir_name or "/" in self.output_dir_name or "\\" in self.output_dir_name:
            raise ValueError("output_dir_name must be a single path segment")


@dataclass(frozen=True)
class AppServerConfig:
    """How to launch and drive ``codex app-server``.

    ``executable`` must resolve to an absolute path: two different Codex
    builds are installed on this machine and their protocol schemas differ.
    ``None`` means "resolve it with :func:`said_done.executor.codex_binary.
    resolve_codex_executable`", which prefers the desktop runtime build that
    the schema in ``docs/codex-appserver-schema`` was generated from.
    """

    executable: str | None = None
    args: tuple[str, ...] = ("app-server", "--listen", "stdio://")
    model: str | None = None
    effort: str | None = None
    client_name: str = "asr_agent"
    client_version: str = "0.1.0"
    service_name: str = "asr_agent"
    startup_timeout_seconds: float = 30.0
    turn_timeout_seconds: float = 900.0
    request_timeout_seconds: float = 60.0
    preflight_command_timeout_seconds: float = 20.0
    interrupt_grace_seconds: float = 15.0
    approval_policy: str = "never"
    network_access: bool = False
    server_preflight: bool = True
    require_server_preflight: bool = True
    probe_shell: str | None = None
    thread_source: str = "asr-agent"
    ephemeral: bool = True

    def __post_init__(self) -> None:
        for name in (
            "startup_timeout_seconds",
            "turn_timeout_seconds",
            "request_timeout_seconds",
            "preflight_command_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0 (timeouts are mandatory)")
        if self.approval_policy not in ("never", "untrusted", "on-request"):
            raise ValueError(f"unsupported approval policy {self.approval_policy!r}")


@dataclass(frozen=True)
class CliExecutorConfig:
    """How to drive ``codex exec`` as a fallback transport."""

    executable: str | None = None
    base_args: tuple[str, ...] = ("exec",)
    extra_args: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    sandbox_mode: str = "workspace-write"
    timeout_seconds: float = 900.0
    json_events: bool = True
    skip_git_repo_check: bool = True
    ephemeral: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0 (timeouts are mandatory)")
        if self.sandbox_mode not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError(f"unsupported sandbox mode {self.sandbox_mode!r}")


@dataclass(frozen=True)
class MockExecutorConfig:
    """Deterministic behaviour of the mock executor."""

    docx_paragraphs: int = 4
    csv_delimiter: str = ","
    csv_encoding: str = "utf-8-sig"
    max_rows: int = 100000

    def __post_init__(self) -> None:
        if self.docx_paragraphs < 1:
            raise ValueError("docx_paragraphs must be >= 1")
        if self.max_rows < 1:
            raise ValueError("max_rows must be >= 1")





