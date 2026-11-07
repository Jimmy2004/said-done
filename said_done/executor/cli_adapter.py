"""Codex CLI adapter (fallback transport).

Same interface as the app-server adapter.  The CLI has no separate
preflight surface, so the preflight here is **LOCAL_ONLY**: the local read-only
checks plus the CLI's own sandbox enforcement at execution time
(``-C <workspace> --sandbox workspace-write``).  Path safety does not rest on
the prompt: the run is confined to the workspace by the Codex sandbox and every
produced file is validated afterwards against the task's expected outputs.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from said_done.contracts import ExecutionResult, PreflightResult, TaskPacket

from .artifact_validator import validated_execution_result
from .codex_binary import CodexBinaryNotFound, resolve_codex_executable
from .config import ArtifactValidatorConfig, CliExecutorConfig, ExecutionConfig, PreflightConfig
from .preflight import resolve_inputs, run_preflight
from .prompt import build_execution_prompt
from .workspace import collect_artifacts, prepare_workspace


class CodexCliError(RuntimeError):
    """The Codex CLI could not be run."""


class CodexCliExecutor:
    """Executor that drives ``codex exec`` instead of the app-server."""

    def __init__(
        self,
        config: CliExecutorConfig | None = None,
        preflight_config: PreflightConfig | None = None,
        execution_config: ExecutionConfig | None = None,
    ) -> None:
        self.config = config or CliExecutorConfig()
        self.execution_config = execution_config or ExecutionConfig()
        self.preflight_config = replace(
            preflight_config or PreflightConfig(),
            output_dir_name=self.execution_config.output_dir_name,
        )
        self.validator_config = ArtifactValidatorConfig(
            output_dir_name=self.execution_config.output_dir_name,
            input_dir_name=self.execution_config.input_dir_name,
        )
        self.preflight_kind = "LOCAL_ONLY"
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    # -------------------------------------------------------------- Executor

    def create_task(self, task: TaskPacket, workspace: Path) -> str:
        with self._lock:
            self._sessions[task.task_id] = {
                "status": "CREATED",
                "workspace": str(Path(workspace)),
                "process": None,
            }
        return task.task_id

    def preflight(self, task: TaskPacket, workspace: Path) -> PreflightResult:
        """Local read-only checks.  There is no server side probe for the CLI."""
        result = run_preflight(task, workspace, self.preflight_config)
        self.preflight_kind = "LOCAL_ONLY" if result.status == "READY" else "LOCAL"
        if result.status == "READY":
            result = PreflightResult(
                status="READY",
                reasons=[
                    "LOCAL_ONLY: the CLI adapter has no Codex read-only probe; the read-only "
                    "guarantee comes from the local checks plus the CLI sandbox at run time"
                ],
                checked_paths=result.checked_paths,
            )
        return result

    def execute(self, task: TaskPacket, workspace: Path) -> ExecutionResult:
        workspace = Path(workspace)
        local = run_preflight(task, workspace, self.preflight_config)
        if local.status != "READY":
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="preflight blocked the task, nothing was executed",
                artifacts=[],
                error="; ".join(local.reasons),
            )
        inputs, errors = resolve_inputs(task, workspace, self.preflight_config)
        if errors:
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="input resolution failed at execution time",
                artifacts=[],
                error="; ".join(errors),
            )

        plan = prepare_workspace(task, workspace, inputs, self.execution_config)
        prompt = build_execution_prompt(task, plan)
        command = self._build_command(workspace, prompt)

        with self._lock:
            self._sessions[task.task_id]["status"] = "RUNNING"
        handle, last_message_path = tempfile.mkstemp(prefix="asr-agent-codex-", suffix=".txt",dir=workspace)
        os.close(handle)
        command += ["-o", last_message_path]
        try:
            process = subprocess.Popen(
                command,
                # Never inherit stdin: `codex exec` appends piped stdin to the
                # prompt and blocks until EOF, and under the MCP server the
                # inherited stdin is the JSON-RPC channel itself.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(workspace),
                start_new_session=os.name!='nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
            )
            with self._lock:
                self._process=process
                self._sessions[task.task_id]['process']=process
                self._sessions[task.task_id]['status']='RUNNING'
            from .process_control import communicate_bounded
            stdout,stderr=communicate_bounded(process,timeout=self.config.timeout_seconds)
            completed=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
            try:
                with Path(last_message_path).open(encoding='utf-8') as stream:
                    summary_from_file=stream.read(1_048_576).strip()
            except OSError:
                summary_from_file=''
        except subprocess.TimeoutExpired:
            from .process_control import stop_process
            stop_process(self._process)
            with self._lock:
                self._sessions[task.task_id]["status"] = "FAILED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex CLI run hit its timeout",
                artifacts=[],
                error=f"the CLI did not finish within {self.config.timeout_seconds:.0f}s",
            )
        except (OSError, CodexBinaryNotFound, RuntimeError) as exc:
            with self._lock:
                self._sessions[task.task_id]["status"] = "FAILED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex CLI could not be run",
                artifacts=[],
                error=type(exc).__name__,
            )
        finally:
            with self._lock:
                self._process=None
            try:
                os.unlink(last_message_path)
            except OSError:
                pass

        summary = summary_from_file
        if not summary:
            summary = self._summary_from_events(completed.stdout)

        artifacts = [str(path) for path in collect_artifacts(workspace, self.execution_config)]
        with self._lock:
            self._sessions[task.task_id]["status"] = "RUNNING"

        if completed.returncode != 0:
            result = ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex CLI exited with an error",
                artifacts=artifacts,
                error=f"exit code {completed.returncode}",
            )
        else:
            result = validated_execution_result(
                task, workspace, self.validator_config,
                summary or "the Codex CLI finished the task", artifacts,
            )
        with self._lock:
            self._sessions[task.task_id]["status"] = result.status
            self._sessions[task.task_id]["result"] = result
        return result

    kind='codex-cli'
    verification='verified'

    @classmethod
    def detect(cls):
        from .registry import executor_info
        return executor_info(cls.kind)

    def _build_command(self, workspace: Path, prompt: str) -> list[str]:
        try:
            executable = str(resolve_codex_executable(self.config.executable))
        except CodexBinaryNotFound:
            executable = self.config.executable
        command = [executable, *self.config.base_args]
        command += ["-C", str(workspace), "-s", self.config.sandbox_mode]
        if self.config.json_events:
            command.append("--json")
        if self.config.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if self.config.ephemeral:
            command.append("--ephemeral")
        if self.config.model:
            command += ["-m", self.config.model]
        if self.config.effort:
            command += ["-c", f'model_reasoning_effort="{self.config.effort}"']
        command += list(self.config.extra_args)
        command.append(prompt)
        return command

    def _summary_from_events(self, stdout: str) -> str:
        """Fall back to the last agent message in the CLI event stream."""
        messages: list[str] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") in {"agentMessage","agent_message"} and item.get("text"):
                messages.append(str(item["text"]))
        return messages[-1] if messages else ""

    def cancel(self, task_id: str) -> None:
        """Best effort cancel: the CLI has no interrupt channel from outside."""
        with self._lock:
            session = self._sessions.get(task_id)
            if session is None:
                return
            session["status"] = "CANCELLED"
            process=session.get('process')
        from .process_control import stop_process
        stop_process(process)

    def get_status(self, task_id: str) -> str:
        with self._lock:
            session = self._sessions.get(task_id)
        return session.get("status", "UNKNOWN") if session else "UNKNOWN"

    def get_result(self, task_id: str) -> ExecutionResult | None:
        with self._lock:
            session = self._sessions.get(task_id)
        return session.get("result") if session else None
