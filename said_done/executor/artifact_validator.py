"""Artifact validation: did the run really produce the promised files?

The check never trusts the narrative.  Every expected output must exist, be
non-trivial, live inside the workspace output directory and actually parse as
the format it claims.  The execution result has to *declare* every mandatory
output, so a missing deliverable cannot hide behind a successful summary.
On top of that the produced content must actually carry the task: a generated
document has to talk about the goal, and a converted file has to carry the
source columns.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

from said_done.contracts import ExecutionResult, TaskPacket

from .config import ArtifactValidatorConfig
from .preflight import is_within

_TEXT_SUFFIXES = {".txt", ".md", ".rst", ".log"}
_CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".sh", ".bash", ".ps1", ".sql", ".bat", ".java", ".go", ".rs"}
_TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".json"}

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "please", "make", "create",
    "generate", "write", "file", "files", "report", "outline", "about", "need", "want", "give",
    "using", "use", "based", "some", "data", "docx", "pdf", "csv", "convert",
}


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _docx_text(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return ""
    parts: list[str] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag == "p":
            parts.append("\n")
    return "".join(parts)


def goal_keywords(goal: str) -> list[str]:
    """Tokens that show the produced content is about the actual goal."""
    lowered = (goal or "").lower()
    tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9\-_+]{2,}", lowered) if t not in _STOPWORDS]
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        for index in range(len(run) - 1):
            tokens.append(run[index : index + 2])
    return tokens


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("latin-1", errors="replace")


def _validate_docx(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    if not zipfile.is_zipfile(path):
        return [f"{path.name} is not a valid DOCX package"], ""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                return [f"{path.name} is not a valid DOCX (no word/document.xml)"], ""
            payload = archive.read("word/document.xml")
    except (zipfile.BadZipFile, OSError) as exc:
        return [f"{path.name} could not be opened as DOCX: {exc}"], ""
    text = _docx_text(payload)
    if not text.strip():
        return [f"{path.name} contains no readable text"], ""
    if len(text.strip()) < max(10, config.min_text_chars // 4):
        return [f"{path.name} contains almost no text"], ""
    return [], text


def _validate_pdf(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    payload = path.read_bytes()
    if not payload.startswith(b"%PDF-"):
        return [f"{path.name} does not start with the PDF header"], ""
    if b"%%EOF" not in payload[-2048:]:
        return [f"{path.name} has no PDF end marker"], ""
    try:
        from pypdf import PdfReader
        reader=PdfReader(path,strict=True)
        if not reader.pages:return [f'{path.name} has no PDF pages'],''
        text='\n'.join(page.extract_text() or '' for page in reader.pages)
        if not text.strip():return [f'{path.name} has no extractable text for goal verification'],''
        return [],text
    except Exception as exc:
        return [f'{path.name} is not a parseable PDF ({type(exc).__name__})'],''


def _validate_csv(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    text = _decode(path.read_bytes())
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [f"{path.name} has no rows"], ""
    widths = {len(row) for row in rows if row}
    if not widths or 0 in widths:
        return [f"{path.name} has a malformed row"], text
    if len(widths) > 1:
        return [f"{path.name} has inconsistent column counts {sorted(widths)}"], text
    if not any(cell.strip() for row in rows for cell in row):
        return [f"{path.name} has no non-empty cell"], text
    return [], text


def _validate_code(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    text = _decode(path.read_bytes())
    if not text.strip():
        return [f"{path.name} is empty"], ""
    if path.suffix.casefold() == ".py":
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return [f"{path.name} is not valid Python: {exc}"], text
    return [], text


def _validate_json(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    text = _decode(path.read_bytes())
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"{path.name} is not valid JSON: {exc}"], text
    return [], text


def _validate_text(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    text = _decode(path.read_bytes())
    if not text.strip():
        return [f"{path.name} is empty"], ""
    return [], text


def validate_file(path: Path, config: ArtifactValidatorConfig) -> tuple[list[str], str]:
    """Validate one file against its declared format; return (errors, text)."""
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        return _validate_docx(path, config)
    if suffix == ".pdf":
        return _validate_pdf(path, config)
    if suffix in (".csv", ".tsv"):
        return _validate_csv(path, config)
    if suffix == ".json":
        return _validate_json(path, config)
    if suffix in _CODE_SUFFIXES:
        return _validate_code(path, config)
    if suffix in _TEXT_SUFFIXES:
        return _validate_text(path, config)
    return [f'{path.name}: unsupported artifact format; cannot verify success'],''


def _looks_like_narrative(text: str, goal: str) -> bool:
    """True when the artifact is essentially the request text repeated."""
    body = " ".join(text.split())
    request = " ".join((goal or "").split())
    if not body or not request:
        return False
    if body == request:
        return True
    if request and (body.startswith(request) or request.startswith(body)):
        return abs(len(body) - len(request)) <= 40
    return False


def _goal_overlap(text: str, goal: str) -> float:
    tokens = goal_keywords(goal)
    if not tokens:
        return 1.0
    haystack = text.lower()
    hits = sum(1 for token in tokens if token in haystack)
    return hits / len(tokens)


def _input_files(workspace: Path, config: ArtifactValidatorConfig) -> list[Path]:
    inputs_dir = Path(workspace) / config.input_dir_name
    if not inputs_dir.is_dir():
        return []
    return sorted(p for p in inputs_dir.rglob("*") if p.is_file())


def _check_conversion(
    task: TaskPacket, workspace: Path, config: ArtifactValidatorConfig, outputs: list[Path]
) -> list[str]:
    """A converted file has to carry the source columns, not just exist."""
    errors: list[str] = []
    sources = _input_files(workspace, config)
    if not sources:
        return errors
    source = sources[0]
    if source.suffix.casefold() not in _TABLE_SUFFIXES:
        return errors
    try:
        from .tabular import TableReadError, read_table

        source_rows = read_table(source)
        for output in outputs:
            if output.suffix.casefold() not in _TABLE_SUFFIXES:
                continue
            output_rows = read_table(output)
            if not source_rows or not output_rows:
                continue
            source_header = [cell.strip() for cell in source_rows[0]]
            output_header = [cell.strip() for cell in output_rows[0]]
            missing = [cell for cell in source_header if cell and cell not in output_header]
            if missing:
                errors.append(
                    f"{output.name} does not carry the source columns {missing[:5]}; the "
                    "conversion lost data"
                )
            if len(output_rows) < len(source_rows):
                errors.append(
                    f"{output.name} has {len(output_rows)} rows but the source has {len(source_rows)}"
                )
    except (TableReadError, OSError, ValueError):
        return errors
    return errors


def _check_derivation(
    task: TaskPacket, workspace: Path, config: ArtifactValidatorConfig, outputs: list[Path]
) -> list[str]:
    """A modified file has to be derived from the file it was supposed to edit."""
    errors: list[str] = []
    sources = _input_files(workspace, config)
    if not sources:
        return errors
    source = sources[0]
    sample = " ".join(_decode(source.read_bytes()).split())[:120]
    if not sample:
        return errors
    for output in outputs:
        body = " ".join(_decode(output.read_bytes()).split())
        if sample and sample not in body:
            errors.append(f"{output.name} does not contain the content of {source.name}")
    return errors


def validate_artifacts(
    task: TaskPacket,
    result: ExecutionResult,
    workspace: Path,
    config: ArtifactValidatorConfig | None = None,
) -> list[str]:
    """Return the list of problems with the produced artifacts ([] means fine)."""
    config = config or ArtifactValidatorConfig()
    workspace = Path(workspace)
    out_root = (workspace / config.output_dir_name).resolve(strict=False)
    errors: list[str] = []
    texts: list[str] = []
    validated: set[str] = set()

    expected: list[Path] = []
    for raw in task.expected_outputs:
        candidate = Path(str(raw).strip())
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (out_root / candidate).resolve(strict=False)
        if not is_within(resolved, out_root):
            errors.append(f"expected output {raw!r} resolves outside {out_root}")
            continue
        expected.append(resolved)

    if not expected:
        errors.append("the task declares no expected output")

    # 1. every mandatory output must exist on disk and be a valid file
    for path in expected:
        if not path.exists():
            errors.append(f"expected output {path.name} was not produced")
            continue
        if not path.is_file():
            errors.append(f"expected output {path.name} is not a regular file")
            continue
        size = path.stat().st_size
        if size < config.min_bytes:
            errors.append(f"expected output {path.name} is only {size} bytes")
            continue
        file_errors, text = validate_file(path, config)
        errors.extend(file_errors)
        texts.append(text)
        validated.add(_norm(path))

    # 2. the declared artifacts must live in the output directory and be valid
    declared: set[str] = set()
    for artifact in result.artifacts:
        path = Path(artifact)
        path = path.resolve(strict=False) if path.is_absolute() else (workspace / path).resolve(strict=False)
        if not is_within(path, out_root):
            errors.append(
                f"artifact {artifact} lies outside the workspace output directory {out_root}"
            )
            continue
        if not path.exists():
            errors.append(f"artifact {artifact} does not exist")
            continue
        if not path.is_file():
            errors.append(f"artifact {artifact} is not a regular file")
            continue
        declared.add(_norm(path))
        if _norm(path) in validated:
            continue
        size = path.stat().st_size
        if size < config.min_bytes:
            errors.append(f"artifact {artifact} is only {size} bytes")
            continue
        file_errors, _text = validate_file(path, config)
        errors.extend(file_errors)
        validated.add(_norm(path))

    # 3. no mandatory deliverable may be missing from the reported artifacts
    for path in expected:
        if _norm(path) not in declared:
            errors.append(
                f"expected output {path.name} is not listed in the execution result artifacts"
            )
    if expected and not result.artifacts:
        errors.append("the execution result lists no artifacts although outputs were produced")

    body = " ".join(texts)
    if expected and not body.strip():
        errors.append("the produced files contain no readable text")
        return errors

    if task.task_type == "CREATE_DOCUMENT":
        if _looks_like_narrative(body, task.goal):
            errors.append("the produced document only repeats the request narrative")
        overlap = _goal_overlap(body, task.goal)
        if overlap < config.min_overlap:
            errors.append(
                f"the produced document does not reflect the stated goal (keyword overlap "
                f"{overlap:.2f} < {config.min_overlap:.2f})"
            )
    elif task.task_type == "CONVERT_FILE":
        errors.extend(_check_conversion(task, workspace, config, expected))
    elif task.task_type == "MODIFY_FILE":
        errors.extend(_check_derivation(task, workspace, config, expected))

    return errors


def validated_execution_result(
    task: TaskPacket,
    workspace: Path,
    config: ArtifactValidatorConfig,
    summary: str | Callable[[], str],
    artifacts: list[str],
) -> ExecutionResult:
    """Build a successful result, downgrading it if artifact validation fails."""
    result = ExecutionResult(
        task_id=task.task_id,
        status="COMPLETED",
        summary=summary() if callable(summary) else summary,
        artifacts=artifacts,
    )
    problems = validate_artifacts(task, result, workspace, config)
    if problems:
        result = ExecutionResult(
            task_id=task.task_id,
            status="PARTIAL" if artifacts else "FAILED",
            summary=summary() if callable(summary) else summary,
            artifacts=artifacts,
            error="; ".join(problems),
        )
    return result
