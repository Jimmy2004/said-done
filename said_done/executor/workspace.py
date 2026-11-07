"""Execution workspace: sandbox layout, input copies and backups.

The executor never hands Codex a path outside the workspace.  User files are
copied *into* the workspace before the model runs, originals stay untouched,
and anything the model is expected to overwrite is backed up first.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from said_done.contracts import TaskPacket

from .config import ExecutionConfig
from .preflight import is_within


@dataclass(frozen=True)
class WorkspacePlan:
    """Where everything lives once execution starts."""

    workspace: Path
    inputs_dir: Path
    outputs_dir: Path
    backups_dir: Path
    input_copies: tuple[tuple[Path, Path], ...] = ()
    output_paths: tuple[Path, ...] = ()
    backups: tuple[Path, ...] = ()


def output_paths_for(task: TaskPacket, workspace: Path, config: ExecutionConfig) -> list[Path]:
    """Absolute paths of the expected outputs inside the workspace."""
    workspace = Path(workspace)
    out_root = (workspace / config.output_dir_name).resolve(strict=False)
    paths: list[Path] = []
    for raw in task.expected_outputs:
        candidate = Path(raw.strip())
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (out_root / candidate).resolve(strict=False)
        if not is_within(resolved, out_root):
            raise ValueError(f"expected output {raw!r} points outside the workspace output directory")
        paths.append(resolved)
    return paths


def backup_file(source: Path, backups_dir: Path) -> Path:
    """Copy *source* into *backups_dir* before it may be modified."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"{source.name}.bak"
    index = 1
    while target.exists():
        target = backups_dir / f"{source.name}.{index}.bak"
        index += 1
    shutil.copy2(source, target)
    return target


def prepare_workspace(
    task: TaskPacket,
    workspace: Path,
    resolved_inputs: list[Path],
    config: ExecutionConfig,
) -> WorkspacePlan:
    """Create the sandbox layout, copy inputs in and back up originals.

    This is the first step that writes anything, and it only ever writes
    inside the execution workspace.
    """
    workspace = Path(workspace)
    inputs_dir = workspace / config.input_dir_name
    outputs_dir = workspace / config.output_dir_name
    backups_dir = workspace / config.backup_dir_name
    for directory in (workspace, inputs_dir, outputs_dir, backups_dir):
        directory.mkdir(parents=True, exist_ok=True)

    copies: list[tuple[Path, Path]] = []
    backups: list[Path] = []
    used: set[str] = set()
    for original in resolved_inputs:
        original = Path(original)
        name = original.name
        candidate_name = name
        counter = 1
        while candidate_name.casefold() in used:
            candidate_name = f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        used.add(candidate_name.casefold())
        target = inputs_dir / candidate_name
        shutil.copy2(original, target)
        target.chmod(target.stat().st_mode & ~0o222)
        copies.append((original, target))
        if task.task_type == "MODIFY_FILE":
            backups.append(backup_file(original, backups_dir))

    outputs = output_paths_for(task, workspace, config)
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)

    return WorkspacePlan(
        workspace=workspace,
        inputs_dir=inputs_dir,
        outputs_dir=outputs_dir,
        backups_dir=backups_dir,
        input_copies=tuple(copies),
        output_paths=tuple(outputs),
        backups=tuple(backups),
    )


def collect_artifacts(workspace: Path, config: ExecutionConfig) -> list[Path]:
    """Every file the model produced inside the workspace output directory."""
    out_root = Path(workspace) / config.output_dir_name
    if not out_root.exists():
        return []
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(out_root):
        for filename in filenames:
            found.append(Path(dirpath) / filename)
    return sorted(found)
