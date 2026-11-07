"""Mock executor: the full pipeline without a model.

The mock still runs the real preflight, the real workspace preparation and
the real artifact validation, so an end-to-end run exercises exactly the same
guards the production adapters do.  What it replaces is only the model call.
"""
from __future__ import annotations

import threading
from pathlib import Path

from said_done.contracts import ExecutionResult, PreflightResult, TaskPacket

from .artifact_validator import validated_execution_result
from .config import ArtifactValidatorConfig, ExecutionConfig, MockExecutorConfig, PreflightConfig
from .preflight import resolve_inputs, run_preflight
from .tabular import TableReadError, read_table, write_csv
from .workspace import collect_artifacts, prepare_workspace


class MockExecutor:
    """Deterministic executor used by the tests and the mock pipeline."""

    def __init__(
        self,
        config: MockExecutorConfig | None = None,
        preflight_config: PreflightConfig | None = None,
        execution_config: ExecutionConfig | None = None,
    ) -> None:
        self.config = config or MockExecutorConfig()
        self.execution_config = execution_config or ExecutionConfig()
        base_preflight = preflight_config or PreflightConfig()
        self.preflight_config = PreflightConfig(
            **{
                **base_preflight.__dict__,
                "output_dir_name": self.execution_config.output_dir_name,
            }
        )
        self.validator_config = ArtifactValidatorConfig(
            output_dir_name=self.execution_config.output_dir_name,
            input_dir_name=self.execution_config.input_dir_name,
        )
        self._status: dict[str, str] = {}
        self._results: dict[str, ExecutionResult] = {}
        self._lock = threading.Lock()

    kind='mock'
    verification='verified'

    @classmethod
    def detect(cls):
        from .registry import executor_info
        return executor_info(cls.kind)

    # ------------------------------------------------------------------ api

    def create_task(self, task: TaskPacket, workspace: Path) -> str:
        with self._lock:
            self._status[task.task_id] = "CREATED"
        return task.task_id

    def preflight(self, task: TaskPacket, workspace: Path) -> PreflightResult:
        result = run_preflight(task, workspace, self.preflight_config)
        with self._lock:
            self._status[task.task_id] = "READY" if result.status == "READY" else "BLOCKED"
        return result

    def execute(self, task: TaskPacket, workspace: Path) -> ExecutionResult:
        with self._lock:
            existing = self._results.get(task.task_id)
        if existing is not None and existing.status == "COMPLETED":
            return existing

        preflight = run_preflight(task, workspace, self.preflight_config)
        if preflight.status != "READY":
            with self._lock:
                self._status[task.task_id] = "BLOCKED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="preflight blocked the task, nothing was executed",
                artifacts=[],
                error="; ".join(preflight.reasons),
            )

        workspace = Path(workspace)
        inputs, errors = resolve_inputs(task, workspace, self.preflight_config)
        if errors:
            with self._lock:
                self._status[task.task_id] = "FAILED"
            return ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="input resolution failed at execution time",
                artifacts=[],
                error="; ".join(errors),
            )

        plan = prepare_workspace(task, workspace, inputs, self.execution_config)
        with self._lock:
            self._status[task.task_id] = "RUNNING"

        try:
            self._produce(task, plan, inputs)
        except Exception as exc:  # noqa: BLE001 - the mock must never crash a test
            result = ExecutionResult(
                task_id=task.task_id,
                status="FAILED",
                summary="mock execution failed",
                artifacts=[],
                error=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                self._status[task.task_id] = "FAILED"
                self._results[task.task_id] = result
            return result

        artifacts = [str(path) for path in collect_artifacts(workspace, self.execution_config)]
        result = validated_execution_result(
            task, workspace, self.validator_config,
            lambda: self._summary(task), artifacts,
        )
        with self._lock:
            self._status[task.task_id] = result.status
            self._results[task.task_id] = result
        return result

    def cancel(self, task_id: str) -> None:
        with self._lock:
            status = self._status.get(task_id)
            if status in (None, "COMPLETED", "FAILED", "CANCELLED"):
                return
            self._status[task_id] = "CANCELLED"

    def get_status(self, task_id: str) -> str:
        with self._lock:
            return self._status.get(task_id, "UNKNOWN")

    def get_result(self, task_id: str) -> ExecutionResult | None:
        with self._lock:
            return self._results.get(task_id)

    # -------------------------------------------------------------- internals

    def _summary(self, task: TaskPacket) -> str:
        return f"mock executor produced {len(task.expected_outputs)} expected output(s) for: {task.goal}"

    def _produce(self, task: TaskPacket, plan, inputs: list[Path]) -> None:
        if task.task_type == "CREATE_DOCUMENT":
            self._produce_document(task, plan)
        elif task.task_type == "CONVERT_FILE":
            self._produce_conversion(task, plan, inputs)
        elif task.task_type == "WRITE_CODE":
            self._produce_code(task, plan)
        elif task.task_type == "MODIFY_FILE":
            self._produce_modification(task, plan, inputs)
        else:  # pragma: no cover - the contract forbids other task types
            raise ValueError(f"unsupported task type {task.task_type!r}")

    def _fact_lines(self, task: TaskPacket) -> list[str]:
        lines = list(task.user_facts)
        lines += list(task.requirements)
        return lines

    def _produce_document(self, task: TaskPacket, plan) -> None:
        facts = self._fact_lines(task)
        for path in plan.output_paths:
            suffix = path.suffix.casefold()
            if suffix == ".docx":
                self._write_docx(path, task, facts)
            elif suffix == ".csv":
                rows = [["item", "detail"]] + [[str(i + 1), fact] for i, fact in enumerate(facts)]
                write_csv(rows, path, self.config.csv_delimiter, self.config.csv_encoding)
            elif suffix in (".txt", ".md"):
                body = [f"# {task.goal}", ""]
                body += [f"- {fact}" for fact in facts]
                body += ["", "(produced by the mock executor, no model was involved)"]
                path.write_text("\n".join(body) + "\n", encoding="utf-8")
            else:
                self._write_docx(path, task, facts)

    def _write_docx(self, path: Path, task: TaskPacket, facts: list[str]) -> None:
        from docx import Document

        document = Document()
        document.add_heading(task.goal, level=0)
        paragraphs = 0
        if facts:
            document.add_paragraph("Content requested by the user:", style="Title")
            for fact in facts[: max(1, self.config.docx_paragraphs)]:
                document.add_paragraph(fact, style="List Bullet")
                paragraphs += 1
        document.add_paragraph("Scope", style="Title")
        document.add_paragraph(
            "This document was produced by the mock executor from the task packet only. "
            "No facts were invented and no model was involved."
        )
        paragraphs += 1
        if paragraphs < self.config.docx_paragraphs:
            document.add_paragraph(
                "The remaining sections were not filled in because the task packet did not "
                "contain the information for them."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(path))

    def _produce_conversion(self, task: TaskPacket, plan, inputs: list[Path]) -> None:
        if not inputs:
            raise TableReadError("no input file to convert")
        source = inputs[0]
        rows = read_table(source)
        if not rows:
            raise TableReadError(f"{source.name} contains no rows")
        if len(rows) > self.config.max_rows:
            rows = rows[: self.config.max_rows]
        for path in plan.output_paths:
            write_csv(rows, path, self.config.csv_delimiter, self.config.csv_encoding)

    def _produce_code(self, task: TaskPacket, plan) -> None:
        lines = [f'"""{task.goal}', ""]
        for requirement in task.requirements:
            lines.append(f"Requirement: {requirement}")
        lines += ["", "Generated by the mock executor; the body is a stub.", '"""', ""]
        for path in plan.output_paths:
            suffix = path.suffix.casefold()
            if suffix == ".py":
                body = "\n".join(lines) + "\n\n\ndef main() -> None:\n    raise NotImplementedError\n"
            elif suffix in (".js", ".ts"):
                body = "\n".join(lines) + "\n\nexport function main() {\n  throw new Error('not implemented');\n}\n"
            else:
                body = "\n".join(lines) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def _produce_modification(self, task: TaskPacket, plan, inputs: list[Path]) -> None:
        if not inputs:
            raise ValueError("MODIFY_FILE needs an input file")
        source = inputs[0]
        payload = source.read_bytes()
        for path in plan.output_paths:
            suffix = path.suffix.casefold()
            path.parent.mkdir(parents=True, exist_ok=True)
            if suffix in (".csv", ".tsv"):
                rows = read_table(source)
                rows.append([f"modified_by_task_{task.task_id}"])
                write_csv(rows, path, self.config.csv_delimiter, self.config.csv_encoding)
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                path.write_bytes(payload)
                continue
            marker = f"\n\n# modified by mock executor for task {task.task_id}\n"
            path.write_text(text + marker, encoding="utf-8")
