"""Codex App Server adapter.

Transport: newline delimited JSON (JSONL) carrying JSON-RPC 2.0 messages
without the ``jsonrpc`` header, over stdio - the shape documented by OpenAI and
confirmed live against the installed binary (see
``docs/codex-appserver-schema/LIVE_EVIDENCE.md``).

Lifecycle: ``initialize`` -> ``initialized`` -> ``thread/start`` -> ``turn/start``.
The thread identifier handed to ``turn/start`` is ``thread.id``; ``thread.sessionId``
is the session *tree* id and must not be used as the thread id.
Output is authoritative in ``item/completed`` (agent message) and
``turn/completed`` (turn status, where only ``completed`` means success).

The preflight is real and read-only: local filesystem checks plus a
``command/exec`` probe that runs inside the server enforced ``readOnly``
sandbox.  No model inference is involved in the preflight.
"""
from __future__ import annotations

import json
import os
from collections import deque
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from said_done.contracts import ExecutionResult, PreflightResult, TaskPacket

from .artifact_validator import validated_execution_result
from .codex_binary import CodexBinaryNotFound, resolve_codex_executable
from .config import (
    AppServerConfig,
    ArtifactValidatorConfig,
    ExecutionConfig,
    PreflightConfig,
)
from .preflight import resolve_inputs, run_preflight
from .prompt import build_execution_prompt
from .workspace import collect_artifacts, prepare_workspace


class CodexAppServerError(RuntimeError):
    """The app-server returned an error or could not be driven."""


class CodexAppServerTimeout(CodexAppServerError):
    """A request or an event did not arrive within its budget."""


def build_readonly_probe_script(paths: list[Path]) -> str:
    """PowerShell that only reads the given files and reports what it saw.

    The file list travels as a JSON array so the script needs no quoting
    gymnastics, and every line of the output is ``OK|<path>`` or
    ``FAIL|<path>|<reason>``.
    """
    payload = json.dumps([str(p) for p in paths]).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Continue'\n"
        f"$items = '{payload}' | ConvertFrom-Json\n"
        "if ($items.Count -eq 0) { Write-Output 'OK|none' }\n"
        "foreach ($item in $items) {\n"
        "  $p = [string]$item\n"
        "  if (Test-Path -LiteralPath $p -PathType Leaf) {\n"
        "    try {\n"
        "      $s = [System.IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')\n"
        "      $s.Close()\n"
        "      Write-Output ('OK|' + $p)\n"
        "    } catch {\n"
        "      Write-Output ('FAIL|' + $p + '|' + $_.Exception.Message)\n"
        "    }\n"
        "  } else {\n"
        "    Write-Output ('FAIL|' + $p + '|missing or not a file')\n"
        "  }\n"
        "}\n"
    )


# BSD head on macOS rejects -c 0; a shell redirection also verifies read-open.
POSIX_PROBE_SCRIPT = """probe_read() {
    if head -c 0 < /dev/null 2>/dev/null; then
        head -c 0 < "$1"
    else
        : < "$1"
    fi
}
if [ "$#" -eq 0 ]; then
    printf '%s\n' 'OK|none'
fi
for p do
    if [ -f "$p" ] && [ -r "$p" ] && probe_read "$p"; then
        printf 'OK|%s\n' "$p"
    else
        printf 'FAIL|%s|missing, not a file, or unreadable\n' "$p"
    fi
done
"""


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None


class AppServerClient:
    """Minimal JSON-RPC client for the Codex app-server."""

    def __init__(
        self,
        executable: Path,
        args: tuple[str, ...],
        client_name: str,
        client_version: str,
        request_timeout: float,
        startup_timeout: float,
        startup_cwd: Path | None = None,
    ) -> None:
        self.executable = Path(executable)
        self.args = tuple(args)
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.startup_cwd=Path(startup_cwd).resolve() if startup_cwd is not None else Path.cwd().resolve()
        self._process: Any = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._notifications = deque(maxlen=4096)
        self._condition = threading.Condition()
        self._next_id = 0
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = self._spawn()
        except OSError as exc:
            raise CodexAppServerError(f"could not start {self.executable}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="codex-app-server", daemon=True)
        self._reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": self.client_name,
                        "version": self.client_version,
                    }
                },
                timeout=self.startup_timeout,
            )
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _spawn(self):
        return subprocess.Popen(
            [str(self.executable), *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
            cwd=str(self.startup_cwd),
            start_new_session=os.name!='nt',
        )

    def close(self) -> None:
        with self._write_lock:
            self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            from .process_control import stop_process
            stop_process(process)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        self._process = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -------------------------------------------------------------- protocol

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            if self._closed or self._process is None or self._process.stdin is None:
                raise CodexAppServerError("the app-server connection is closed")
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        from .process_control import MAX_OUTPUT, stop_process
        total=0
        while True:
            line=process.stdout.readline(MAX_OUTPUT+1)
            if not line:
                break
            total+=len(line)
            if total>MAX_OUTPUT:
                stop_process(process)
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(message)
            except Exception:  # noqa: BLE001 - never kill the reader thread
                continue

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            with self._pending_lock:
                pending = self._pending.pop(message["id"], None)
            if pending is not None:
                pending.result = message.get("result")
                pending.error = message.get("error")
                pending.event.set()
            return
        if "id" in message and "method" in message:
            self._answer_server_request(message)
            return
        if "method" in message:
            with self._condition:
                self._notifications.append(message)
                self._condition.notify_all()

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        """Default-deny responder: approvals are declined, unknown requests error."""
        method = str(message.get("method", ""))
        if method.endswith("requestApproval"):
            self._send({"id": message["id"], "result": {"decision": "decline"}})
            return
        self._send(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": f"asr-agent does not support {method}"},
            }
        )

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending()
            self._pending[request_id] = pending
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        budget = timeout if timeout is not None else self.request_timeout
        if not pending.event.wait(budget):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise CodexAppServerTimeout(f"timed out after {budget:.1f}s waiting for {method}")
        if pending.error:
            raise CodexAppServerError(f"{method} failed")
        return pending.result or {}

    def send_request_fire_and_forget(self, method: str, params: dict[str, Any]) -> None:
        """Send a request without waiting for the response (used by cancel)."""
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def wait_for(
        self,
        method: str,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for notification in self._notifications:
                    if notification.get("method") == method and predicate(notification):
                        return notification
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexAppServerTimeout(
                        f"timed out after {timeout:.1f}s waiting for {method}"
                    )
                self._condition.wait(remaining)

    def notifications(self, method: str) -> list[dict[str, Any]]:
        with self._condition:
            return [n for n in self._notifications if n.get("method") == method]


class CodexAppServerExecutor:
    """Executor that drives the Codex App Server."""
    kind='codex-app-server'
    verification='verified'

    @classmethod
    def detect(cls):
        from .registry import executor_info
        return executor_info(cls.kind)

    def __init__(
        self,
        config: AppServerConfig | None = None,
        preflight_config: PreflightConfig | None = None,
        execution_config: ExecutionConfig | None = None,
    ) -> None:
        self.config = config or AppServerConfig()
        self.execution_config = execution_config or ExecutionConfig()
        self.preflight_config = replace(
            preflight_config or PreflightConfig(),
            output_dir_name=self.execution_config.output_dir_name,
        )
        self.validator_config = ArtifactValidatorConfig(
            output_dir_name=self.execution_config.output_dir_name,
            input_dir_name=self.execution_config.input_dir_name,
        )
        self.preflight_kind = "UNKNOWN"
        self._client: AppServerClient | None = None
        self._client_workspace: Path | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._ready_cache: dict[tuple[str, str], tuple[bool, list[str]]] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- setup

    def executable_path(self) -> Path:
        return resolve_codex_executable(self.config.executable)

    def _ensure_client(self, workspace: Path | None = None) -> AppServerClient:
        with self._lock:
            root=Path(workspace).resolve() if workspace is not None else (self._client_workspace or Path.cwd().resolve())
            if self._client is not None and self._client.running:
                if self._client_workspace==root:
                    return self._client
                if any(s.get('status') in {'RUNNING','inProgress'} for s in self._sessions.values()):
                    raise CodexAppServerError('cannot change workspace while a task is running')
                self._client.close()
                self._client=None
            self._ready_cache.clear()
            executable = self.executable_path()
            client = AppServerClient(
                executable=executable,
                args=self.config.args,
                client_name=self.config.client_name,
                client_version=self.config.client_version,
                request_timeout=self.config.request_timeout_seconds,
                startup_timeout=self.config.startup_timeout_seconds,
                startup_cwd=root,
            )
            client.start()
            self._client = client
            self._client_workspace=root
            return client

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def __enter__(self) -> "CodexAppServerExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- Executor

    def create_task(self, task: TaskPacket, workspace: Path) -> str:
        client = self._ensure_client(workspace)
        params: dict[str, Any] = {
            "cwd": str(Path(workspace)),
            "sandbox": "workspace-write",
            "approvalPolicy": self.config.approval_policy,
            "serviceName": self.config.service_name,
            "ephemeral": self.config.ephemeral,
        }
        if self.config.model:
            params["model"] = self.config.model
        result = client.request(
            "thread/start", params, timeout=self.config.request_timeout_seconds
        )
        thread = result.get("thread") or {}
        # thread.id identifies this thread; thread.sessionId is the session tree id.
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexAppServerError("thread/start did not return a thread id")
        with self._lock:
            self._sessions[task.task_id] = {
                "thread_id": thread_id,
                "turn_id": None,
                "status": "CREATED",
                "workspace": str(Path(workspace)),
            }
        return task.task_id

    def preflight(self, task: TaskPacket, workspace: Path) -> PreflightResult:
        """Local read-only checks plus a server side readOnly sandbox probe."""
        local = self.local_preflight(task, workspace)
        if local.status != "READY":
            self.preflight_kind = "LOCAL"
            return local
        if not self.config.server_preflight:
            self.preflight_kind = "LOCAL_ONLY"
            return PreflightResult(
                status="BLOCKED",
                reasons=[
                    "LOCAL_ONLY: this executor performs local checks only, so no Codex read-only "
                    "preflight was run; real execution stays disabled"
                ],
                checked_paths=local.checked_paths,
            )
        ok, reasons = self._server_ready(task, workspace)
        if not ok:
            self.preflight_kind = "SERVER"
            return PreflightResult(
                status="BLOCKED",
                reasons=reasons or ["the Codex read-only preflight found a problem"],
                checked_paths=local.checked_paths,
            )
        self.preflight_kind = "LOCAL+SERVER"
        return PreflightResult(
            status="READY",
            reasons=["local checks passed and the Codex read-only sandbox confirmed the inputs"],
            checked_paths=local.checked_paths,
        )

    def local_preflight(self, task: TaskPacket, workspace: Path) -> PreflightResult:
        """The pure local part of the preflight: no server, no model, no writes."""
        return run_preflight(task, workspace, self.preflight_config)

    def _server_ready(self, task: TaskPacket, workspace: Path) -> tuple[bool, list[str]]:
        """Run the read-only server probe for this task and workspace.

        The verdict is remembered per (task, workspace) pair so one task can
        never inherit another task's readiness.
        """
        key = (task.task_id, str(Path(workspace)))
        with self._lock:
            cached = self._ready_cache.get(key)
        if cached is not None:
            return cached
        reasons: list[str] = []
        ok = False
        try:
            client = self._ensure_client(workspace)
            inputs, _errors = resolve_inputs(task, Path(workspace), self.preflight_config)
            ok, reasons = self._probe_readonly(client, Path(workspace), inputs)
        except CodexBinaryNotFound as exc:
            reasons = [f"LOCAL_ONLY: no Codex binary available ({exc}); real execution disabled"]
        except (CodexAppServerError, OSError) as exc:
            reasons = [f"the Codex read-only preflight could not be run: {exc}"]
        with self._lock:
            self._ready_cache[key] = (ok, reasons)
        return ok, reasons

    def _probe_readonly(
        self, client: AppServerClient, workspace: Path, inputs: list[Path]
    ) -> tuple[bool, list[str]]:
        if os.name == 'nt':
            command = [self.config.probe_shell or "powershell.exe", "-NoProfile",
                       "-NonInteractive", "-Command", build_readonly_probe_script(inputs)]
        else:
            command = [self.config.probe_shell or "/bin/sh", "-c", POSIX_PROBE_SCRIPT,
                       "said-done-probe", *map(str, inputs)]
        params = {
            "command": command,
            "cwd": str(workspace),
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "timeoutMs": int(self.config.preflight_command_timeout_seconds * 1000),
        }
        result = client.request(
            "command/exec",
            params,
            timeout=self.config.preflight_command_timeout_seconds + 10.0,
        )
        stdout = str(result.get("stdout", ""))
        exit_code = result.get("exitCode")
        reasons: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("FAIL|"):
                parts = line.split("|", 2)
                reason = parts[2] if len(parts) > 2 else "unreadable"
                reasons.append(f"the Codex sandbox could not read {parts[1]}: {reason}")
        if exit_code != 0:
            stderr = str(result.get("stderr", "")).strip()
            reasons.append(
                f"the Codex read-only probe exited with {exit_code}"
            )
        return (not reasons), reasons

    def execute(self, task: TaskPacket, workspace: Path) -> ExecutionResult:
        workspace = Path(workspace)
        if not self.config.server_preflight:
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="real execution is disabled for a LOCAL_ONLY executor",
                artifacts=[],
                error="LOCAL_ONLY: the Codex read-only preflight was not run",
            )
        local = self.local_preflight(task, workspace)
        if local.status != "READY":
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="preflight blocked the task, nothing was executed",
                artifacts=[],
                error="; ".join(local.reasons),
            )
        # The server must confirm READY for *this* task and workspace before
        # anything is written.
        ready, reasons = self._server_ready(task, workspace)
        if not ready:
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex read-only preflight did not pass, nothing was executed",
                artifacts=[],
                error="; ".join(reasons) or "the Codex read-only preflight did not pass",
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
        client = self._ensure_client(workspace)
        with self._lock:
            session = self._sessions.get(task.task_id)
        if session is None:
            self.create_task(task, workspace)
            with self._lock:
                session = self._sessions[task.task_id]

        prompt = build_execution_prompt(task, plan)
        params: dict[str, Any] = {
            "threadId": session["thread_id"],
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(workspace),
            "approvalPolicy": self.config.approval_policy,
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(workspace)],
                "networkAccess": self.config.network_access,
            },
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.config.effort:
            params["effort"] = self.config.effort

        with self._lock:
            session["status"] = "RUNNING"
        try:
            turn_result = client.request(
                "turn/start", params, timeout=self.config.request_timeout_seconds
            )
        except (CodexAppServerError, OSError) as exc:
            with self._lock:
                session["status"] = "FAILED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex turn could not be started",
                artifacts=[],
                error=type(exc).__name__,
            )
        turn = turn_result.get("turn") or {}
        turn_id = turn.get("id")
        with self._lock:
            session["turn_id"] = turn_id

        try:
            completed = client.wait_for(
                "turn/completed",
                lambda n: self._matches_turn(n, session["thread_id"], turn_id),
                self.config.turn_timeout_seconds,
            )
        except CodexAppServerTimeout as exc:
            note = self._stop_turn(client, session, turn_id)
            with self._lock:
                session["status"] = "FAILED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="the Codex turn did not finish within its timeout",
                artifacts=[],
                error=f"{exc}; {note}",
            )

        turn_payload = completed.get("params", {}).get("turn", {}) or {}
        turn_status = turn_payload.get("status")
        messages = self._collect_messages(client, session["thread_id"], turn_id)
        summary = messages[-1] if messages else f"the Codex turn ended with status {turn_status}"

        artifacts = [str(path) for path in collect_artifacts(workspace, self.execution_config)]
        with self._lock:
            session["status"] = turn_status or "UNKNOWN"

        if turn_status == "interrupted":
            result = ExecutionResult(
                task_id=task.task_id,
                status="CANCELLED",
                summary=summary,
                artifacts=artifacts,
            )
        elif turn_status == "failed":
            error = (turn_payload.get("error") or {}).get("message", "the Codex turn failed")
            result = ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary=summary,
                artifacts=artifacts,
                error="Codex turn failed",
            )
        elif turn_status == "completed":
            result = validated_execution_result(
                task, workspace, self.validator_config,
                summary, artifacts,
            )
        else:
            # Only "completed" counts as success; anything else fails closed.
            result = ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary=summary,
                artifacts=artifacts,
                error=f"the Codex turn ended with the unexpected status {turn_status!r}",
            )
        with self._lock:
            session["result"] = result
        return result

    def _stop_turn(self, client: AppServerClient, session: dict[str, Any], turn_id: str | None) -> str:
        """Stop a runaway turn before returning.

        The interrupt is cooperative, so it is followed by a bounded wait for
        the turn to report back.  If the turn cannot be confirmed stopped the
        app-server process is terminated, because a Codex turn that keeps
        running would keep modifying files while the broker starts the next
        task.
        """
        thread_id = session.get("thread_id")
        note = ""
        stopped = False
        if thread_id and turn_id:
            try:
                client.send_request_fire_and_forget(
                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                )
            except CodexAppServerError:
                pass
            try:
                client.wait_for(
                    "turn/completed",
                    lambda n: self._matches_turn(n, thread_id, turn_id),
                    self.config.interrupt_grace_seconds,
                )
                stopped = True
                note = "the turn was interrupted and confirmed stopped"
            except CodexAppServerTimeout:
                note = "the turn could NOT be confirmed stopped"
        else:
            note = "the turn id was unknown, so the turn could not be interrupted"
        client.close()
        with self._lock:
            if self._client is client:
                self._client = None
        note += "; the app-server process was terminated so it can no longer modify files"
        if not stopped:
            note += "; the side effects of the interrupted turn are unknown"
        return note

    def _matches_turn(self, notification: dict[str, Any], thread_id: str, turn_id: str | None) -> bool:
        params = notification.get("params", {}) or {}
        if params.get("threadId") != thread_id:
            return False
        if turn_id is None:
            return True
        return (params.get("turn") or {}).get("id") == turn_id

    def _collect_messages(
        self, client: AppServerClient, thread_id: str, turn_id: str | None
    ) -> list[str]:
        messages: list[str] = []
        for notification in client.notifications("item/completed"):
            params = notification.get("params", {}) or {}
            if params.get("threadId") != thread_id:
                continue
            if turn_id is not None and params.get("turnId") != turn_id:
                continue
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text"):
                messages.append(str(item["text"]))
        return messages

    def cancel(self, task_id: str) -> None:
        """Best effort cooperative cancel.

        ``turn/interrupt`` is cooperative and there is no synchronous cancel in
        the protocol: the interrupt is sent without waiting for the response and
        the local record is marked CANCELLED immediately, while the server side
        turn may still be winding down.
        """
        with self._lock:
            session = self._sessions.get(task_id)
        if session is None:
            return
        thread_id = session.get("thread_id")
        turn_id = session.get("turn_id")
        if thread_id and turn_id:
            client = self._client
            if client is not None and client.running:
                try:
                    client.send_request_fire_and_forget(
                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                    )
                except CodexAppServerError:
                    pass
        with self._lock:
            session["status"] = "CANCELLED"

    def get_status(self, task_id: str) -> str:
        with self._lock:
            session = self._sessions.get(task_id)
        return session.get("status", "UNKNOWN") if session else "UNKNOWN"

    def get_result(self, task_id: str) -> ExecutionResult | None:
        with self._lock:
            session = self._sessions.get(task_id)
        return session.get("result") if session else None
