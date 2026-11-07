"""Prompt construction for the real Codex adapters.

The prompt is assembled from the task packet only.  It separates what the
user said from what the system inferred, and it pins every output to an
absolute path inside the workspace so path safety does not depend on the
model cooperating.
"""
from __future__ import annotations

from pathlib import Path

from said_done.contracts import TaskPacket

from .config import ExecutionConfig
from .workspace import WorkspacePlan


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def build_preflight_prompt(
    task: TaskPacket, resolved_inputs: list[Path], output_paths: list[Path]
) -> str:
    """Prompt asking Codex for a read-only preflight verdict."""
    lines = [
        "You are a read-only preflight checker for a task that has not started yet.",
        "Do not create, modify or delete any file. Do not run commands that write.",
        "",
        f"Goal: {_one_line(task.goal)}",
    ]
    if task.requirements:
        lines.append("")
        lines.append("Requirements stated by the user:")
        lines.extend(f"- {_one_line(r)}" for r in task.requirements)
    if task.user_facts:
        lines.append("")
        lines.append("Facts stated by the user:")
        lines.extend(f"- {_one_line(f)}" for f in task.user_facts)
    if task.forbidden_assumptions:
        lines.append("")
        lines.append("The task must not assume:")
        lines.extend(f"- {_one_line(f)}" for f in task.forbidden_assumptions)
    if resolved_inputs:
        lines.append("")
        lines.append("Input files that must exist and be readable:")
        lines.extend(f"- {path}" for path in resolved_inputs)
    else:
        lines.append("")
        lines.append("Input files: none, the task creates new content only.")
    if output_paths:
        lines.append("")
        lines.append("Files that must be produced:")
        lines.extend(f"- {path}" for path in output_paths)
    lines += [
        "",
        "Answer with a single JSON object and nothing else:",
        '{"ready": true|false, "reasons": ["..."], "missing_inputs": ["..."], "ambiguities": ["..."]}',
        "Set ready to false when an input is missing or unreadable, when the goal is still "
        "ambiguous, or when there is not enough information to produce the listed outputs.",
    ]
    return "\n".join(lines)


def build_execution_prompt(task: TaskPacket, plan: WorkspacePlan) -> str:
    """Prompt asking Codex to carry out the approved task inside the sandbox."""
    lines = [
        "You are executing one task that has already been approved by the user.",
        f"Goal: {_one_line(task.goal)}",
        "",
        "Facts stated by the user (this is the only ground truth):",
    ]
    lines.extend(f"- {_one_line(f)}" for f in (task.user_facts or ["(none provided)"]))
    lines += ["", "Requirements:"]
    lines.extend(f"- {_one_line(r)}" for r in (task.requirements or ["(none provided)"]))
    if task.historical_context:
        lines += ["", "Earlier context supplied with the request:"]
        lines.extend(f"- {_one_line(c)}" for c in task.historical_context)
    if task.inferences:
        lines += ["", "Inferences made by the system (not user facts, keep them separate):"]
        lines.extend(f"- {_one_line(i)}" for i in task.inferences)
    if task.defaults:
        lines += ["", "Defaults already decided for this task:"]
        lines.extend(f"- {key}: {_one_line(value)}" for key, value in task.defaults.items())
    if plan.input_copies:
        lines += ["", "Input files available in the workspace (read-only copies):"]
        lines.extend(f"- {copy}" for _original, copy in plan.input_copies)
    else:
        lines += ["", "Input files: none, create new content only."]
    lines += ["", "Required output files (write exactly to these paths):"]
    lines.extend(f"Output file: {path}" for path in plan.output_paths)
    lines += [
        "",
        "Rules:",
        "- Use only the input files listed above and the facts listed above.",
        "- Never invent numbers, names, dates, measurements or file contents that are not in the "
        "facts or the input files.",
        "- Write each required output to exactly the path in its 'Output file:' line.",
        "- Do not read or write anything outside the workspace directory.",
        "- If the request is still ambiguous with the information given, stop and state what is "
        "missing instead of guessing.",
    ]
    if task.forbidden_assumptions:
        lines += ["", "Explicitly forbidden assumptions:"]
        lines.extend(f"- {_one_line(f)}" for f in task.forbidden_assumptions)
    return "\n".join(lines)
