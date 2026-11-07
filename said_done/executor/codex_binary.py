"""Locate the Codex executable that the adapters spawn.

Resolution order:

1. an explicitly configured path (absolute, or relative to the working
   directory),
2. the ``ASR_AGENT_CODEX_BIN`` environment variable,
3. a ``PATH`` lookup using the executable names of the current platform.

The bare name ``codex`` is never handed to the operating system unresolved:
on Windows ``CreateProcess`` and ``shutil.which`` can disagree about which
install they pick, so the executable is always resolved to an absolute path
before it is spawned.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ENV_VAR = "ASR_AGENT_CODEX_BIN"


class CodexBinaryNotFound(RuntimeError):
    """No usable Codex executable could be located."""


def platform_codex_names() -> tuple[str, ...]:
    """The executable names to look for on ``PATH`` on this platform."""
    if os.name == "nt":
        return ("codex.exe", "codex.cmd", "codex")
    return ("codex",)


def _looks_like_path(value: str) -> bool:
    if os.path.isabs(value):
        return True
    separators = {os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return any(sep in value for sep in separators)


def _resolve_candidate(value: str) -> Path | None:
    """Turn one configured value into an existing file, or ``None``."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if _looks_like_path(value):
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return candidate.resolve(strict=False)
        return None
    found = shutil.which(value)
    if found:
        return Path(found).resolve(strict=False)
    return None


def resolve_codex_executable(preferred: str | os.PathLike[str] | None = None) -> Path:
    """Return the absolute path of the Codex executable to spawn.

    An explicitly configured value wins, then the ``ASR_AGENT_CODEX_BIN``
    environment variable, then the platform's ``PATH`` lookup.  A configured
    value may be a full path or a bare command name; a bare name is resolved
    through ``PATH`` so the same configuration works on every platform.
    """
    candidates: list[str] = []
    if preferred:
        candidates.append(os.fspath(preferred))
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        candidates.append(env_value)

    for candidate in candidates:
        resolved = _resolve_candidate(candidate)
        if resolved is not None:
            return resolved

    for name in platform_codex_names():
        found = shutil.which(name)
        if found:
            return Path(found).resolve(strict=False)

    raise CodexBinaryNotFound(
        "no Codex executable found; set the codex_executable setting or the "
        f"{ENV_VAR} environment variable to an absolute path, or put 'codex' on PATH"
    )


def codex_version(executable: Path | None = None, timeout: float = 10.0) -> str:
    """Return the ``codex --version`` banner of the resolved binary."""
    path = Path(executable) if executable else resolve_codex_executable()
    try:
        completed = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexBinaryNotFound(f"could not run {path} --version: {exc}") from exc
    return (completed.stdout or completed.stderr or "").strip()
