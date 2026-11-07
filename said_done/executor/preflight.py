"""Bounded, local, read-only preflight.

The preflight answers one question: *is it safe and unambiguous enough to let
Codex touch the disk?*  It never writes, never talks to a model and never
runs a tool.  Everything it checks is a local read with a hard budget on
files, depth and wall-clock time.  Anything it cannot decide locally makes
the task BLOCKED rather than "probably fine".
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from typing import Iterable

from said_done.contracts import PreflightResult, TaskPacket

from .config import PreflightConfig

_WILDCARDS = "*?["


class ScanBudget:
    """Finite budget for the filesystem scan performed by the preflight."""

    def __init__(self, max_files: int, max_seconds: float) -> None:
        self.max_files = max_files
        self.max_seconds = max_seconds
        self.files_seen = 0
        self.deadline = time.monotonic() + max_seconds
        self.exhausted = False

    def visit(self) -> bool:
        """Consume one file from the budget; False when the budget is gone."""
        if time.monotonic() > self.deadline:
            self.exhausted = True
            return False
        self.files_seen += 1
        if self.files_seen > self.max_files:
            self.exhausted = True
            return False
        return True


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def is_within(child: Path, parent: Path) -> bool:
    """True when *child* resolves inside *parent* (case-insensitive on Windows)."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def _has_wildcard(value: str) -> bool:
    return any(ch in value for ch in _WILDCARDS)


def _roots(config: PreflightConfig, workspace: Path) -> list[Path]:
    roots = [Path(r).expanduser() for r in config.readonly_roots]
    roots.append(workspace)
    return roots


def _scan_for_name(root: Path, name: str, budget: ScanBudget, max_depth: int) -> list[Path]:
    """Bounded recursive search for files called *name* under *root*."""
    hits: list[Path] = []
    target = name.casefold()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if not budget.visit():
                return hits
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if entry.name.casefold() == target:
                        hits.append(Path(entry.path))
            except OSError:
                continue
    return hits


def _resolve_one(
    raw: str, workspace: Path, roots: list[Path], config: PreflightConfig, budget: ScanBudget
) -> tuple[Path | None, str | None]:
    """Resolve one declared input to exactly one existing file."""
    candidate = Path(raw).expanduser()
    if str(raw).startswith(('\\\\','//')):
        return None,'network input paths are not supported by bounded local preflight'
    if _has_wildcard(raw):
        return None,'wildcard input paths are ambiguous and unsupported'
    direct: list[Path] = []
    if candidate.is_absolute():
        if candidate.exists():
            direct = [candidate]
        else:
            return None,f'explicit input {raw!r} does not exist; no substitute search permitted'
    else:
        in_workspace = workspace / candidate
        if in_workspace.exists():
            direct = [in_workspace]
        elif len(candidate.parts)>1:
            return None,f'explicit relative input {raw!r} does not exist'

    if direct:
        resolved = direct[0].resolve(strict=False)
        if not resolved.is_file():
            return None, f"input {raw!r} is not a regular file"
        if not any(is_within(resolved, root) for root in roots):
            return None, f"input {raw!r} is outside the allowed read-only roots"
        return resolved, None

    # Fall back to a bounded unique-name search inside the allowed roots.
    name = candidate.name
    matches = []
    for root in roots:
        if not root.exists():
            continue
        for hit in _scan_for_name(root, name, budget, config.max_scan_depth):
            resolved = hit.resolve(strict=False)
            if any(is_within(resolved, r) for r in roots):
                matches.append(resolved)
    matches = sorted(set(matches))
    if not matches:
        return None, f"input {raw!r} was not found in the allowed roots"
    if len(matches) > 1:
        where = ", ".join(str(m) for m in matches[:5])
        return None, f"input {raw!r} matches {len(matches)} files ({where}); the target is ambiguous"
    return matches[0], None


def resolve_inputs(
    task: TaskPacket, workspace: Path, config: PreflightConfig
) -> tuple[list[Path], list[str]]:
    """Resolve every declared input to exactly one file, or explain why not."""
    workspace = Path(workspace)
    roots = _roots(config, workspace)
    budget = ScanBudget(config.max_scan_files, config.max_scan_seconds)
    resolved: list[Path] = []
    errors: list[str] = []

    for raw in task.input_paths:
        path, error = _resolve_one(raw, workspace, roots, config, budget)
        if error:
            errors.append(error)
            continue
        assert path is not None
        if path.suffix.casefold() in {'.xlsx','.docx','.pptx'} and not zipfile.is_zipfile(path):
            errors.append(f'input {path.name} does not match its Office file type')
            continue
        if path.suffix.casefold()=='.pdf':
            with path.open('rb') as stream:
                if not stream.read(5).startswith(b'%PDF-'):
                    errors.append(f'input {path.name} does not match PDF format');continue
        if task.task_type=='CONVERT_FILE' and path.suffix.casefold() not in {'.xlsx','.csv','.tsv','.json'}:
            errors.append(f'input type {path.suffix} is unsupported for V0.1 table conversion');continue
        resolved.append(path)

    if budget.exhausted:
        errors.append(
            f"input scan hit its budget (max_scan_files={config.max_scan_files}, "
            f"max_scan_seconds={config.max_scan_seconds}); resolution is not trustworthy"
        )

    seen: dict[str, Path] = {}
    for path in resolved:
        key = _norm(path)
        if key in seen:
            errors.append(f"input {path} is declared more than once")
        else:
            seen[key] = path

    return resolved, errors


def resolve_outputs(
    task: TaskPacket, workspace: Path, config: PreflightConfig
) -> tuple[list[Path], list[str]]:
    """Map expected outputs to safe locations inside the workspace."""
    workspace = Path(workspace)
    out_root = (workspace / config.output_dir_name).resolve(strict=False)
    outputs: list[Path] = []
    errors: list[str] = []

    for raw in task.expected_outputs:
        name = raw.strip()
        if not name:
            errors.append("an expected output is empty")
            continue
        candidate = Path(name)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            if not is_within(resolved, out_root):
                errors.append(
                    f"expected output {raw!r} points outside {out_root}; outputs must stay in the "
                    "workspace output directory"
                )
                continue
        else:
            if ".." in candidate.parts:
                errors.append(f"expected output {raw!r} escapes the workspace with '..'")
                continue
            resolved = (out_root / candidate).resolve(strict=False)
            if not is_within(resolved, out_root):
                errors.append(f"expected output {raw!r} escapes the workspace output directory")
                continue

        if resolved.suffix == "":
            errors.append(
                f"expected output {raw!r} has no file extension, so the expected format is ambiguous"
            )
            continue
        if resolved.exists() and resolved.is_dir():
            errors.append(f"expected output {raw!r} is an existing directory")
            continue
        outputs.append(resolved)

    seen: set[str] = set()
    for path in outputs:
        key = _norm(path)
        if key in seen:
            errors.append(f"expected output {path} is declared more than once")
        seen.add(key)

    return outputs, errors


def check_dependencies(config: PreflightConfig) -> list[str]:
    """Verify the tools and Python modules the task needs are present."""
    errors: list[str] = []
    for tool in config.required_tools:
        if shutil.which(tool) is None:
            errors.append(f"required tool {tool!r} was not found on PATH")
    for module in config.required_modules:
        try:
            found = find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            errors.append(f"required python module {module!r} is not importable")
    return errors


def _writable_ancestor(path: Path) -> Path | None:
    current = path
    for _ in range(64):
        if current.exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def check_workspace(workspace: Path) -> list[str]:
    """Check that the execution workspace exists (or can exist) and is writable."""
    errors: list[str] = []
    workspace = Path(workspace)
    if workspace.exists():
        if not workspace.is_dir():
            errors.append(f"execution workspace {workspace} is not a directory")
            return errors
    else:
        ancestor = _writable_ancestor(workspace)
        if ancestor is None:
            errors.append(f"execution workspace {workspace} cannot be created")
            return errors
        if not os.access(ancestor, os.W_OK):
            errors.append(f"execution workspace {workspace} is not writable")
            return errors
        return errors
    if not os.access(workspace, os.W_OK | os.X_OK):
        errors.append(f"execution workspace {workspace} is not writable")
    return errors


def check_goal(task: TaskPacket, config: PreflightConfig) -> list[str]:
    """Check that the request is specific enough to produce something."""
    errors: list[str] = []
    goal = (task.goal or "").strip()
    if len(goal) < config.min_goal_chars:
        errors.append(
            f"goal is not specific enough ({len(goal)} chars, need at least {config.min_goal_chars})"
        )
    context = task.requirements + task.user_facts + task.historical_context
    if not context:
        errors.append(
            "the task carries no requirements, facts or context, so the intended artifact cannot "
            "be determined without guessing"
        )
    if task.task_type in ("CONVERT_FILE", "MODIFY_FILE") and not task.input_paths:
        errors.append(f"{task.task_type} requires input files but none were provided")
    if task.task_type == "CREATE_DOCUMENT" and task.input_paths:
        errors.append(
            "CREATE_DOCUMENT must not declare input files; the task would depend on data that the "
            "user never mentioned"
        )
    return errors


def run_preflight(
    task: TaskPacket, workspace: Path, config: PreflightConfig | None = None
) -> PreflightResult:
    """Run the full read-only preflight and return the verdict."""
    config = config or PreflightConfig()
    workspace = Path(workspace)
    reasons: list[str] = []
    checked: list[str] = []

    if config.allow_tool_calls:
        reasons.append(
            "tool-based preflight is not supported by this executor; refusing to guess instead of "
            "calling tools"
        )
    if config.max_tool_calls != 0:
        reasons.append(
            f"max_tool_calls={config.max_tool_calls} was requested but the local preflight never "
            "calls tools; the budget cannot be honoured"
        )

    reasons.extend(check_goal(task, config))
    reasons.extend(check_dependencies(config))
    reasons.extend(check_workspace(workspace))

    inputs, input_errors = resolve_inputs(task, workspace, config)
    reasons.extend(input_errors)
    outputs, output_errors = resolve_outputs(task, workspace, config)
    reasons.extend(output_errors)

    input_keys = {_norm(p) for p in inputs}
    for out in outputs:
        if _norm(out) in input_keys:
            reasons.append(f"expected output {out} would overwrite an input file")

    checked.extend(str(p) for p in inputs)
    checked.extend(str(p) for p in outputs)
    checked.append(str(workspace))

    return PreflightResult(status="READY" if not reasons else "BLOCKED", reasons=reasons, checked_paths=checked)
