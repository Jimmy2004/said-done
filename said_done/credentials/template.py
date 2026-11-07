"""The template the user fills in by hand (docs/DESIGN.md §2).

The file is created 0600 inside a 0700 inbox. Only the *path* is ever handed
back to the host agent -- the agent must not read this file.
"""
from __future__ import annotations

import os
from pathlib import Path

from .paths import ensure_private_dir, inbox_dir

TEMPLATE_TEXT = """# 填好后保存，然后告诉 Agent “填好了”。不要把 key 发到聊天里。
# 以 # 开头的行会被忽略。
base_url =
api_key =
# 可选：openai-compatible（OpenAI Responses API）| anthropic | chat-completions（/chat/completions：MiniMax、DeepSeek、OpenRouter、各类网关等）；留空则自动探测（向 /models、/chat/completions、/responses、/messages 发送不带模型的最小请求，不消耗 token）
api_style =
"""


def template_path(profile: str) -> Path:
    from .paths import import_path
    return import_path(profile)


def write_template(profile: str) -> tuple[Path, bool, list[str]]:
    """Create the template file.

    Returns ``(path, created, warnings)``. An existing file is never
    overwritten -- the user may already have typed their key into it.
    """
    directory = ensure_private_dir(inbox_dir())
    path = template_path(profile)
    warnings: list[str] = []
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        warnings.append('模板文件已存在，未覆盖；如果要重新填写请手动删除该文件。')
        return path, False, warnings
    except OSError as exc:
        from .errors import CredentialError
        raise CredentialError(
            f'无法创建模板文件（{type(exc).__name__}）') from None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(TEMPLATE_TEXT)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        created = True
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path, created, warnings
