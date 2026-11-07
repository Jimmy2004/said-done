"""Secure reader, parser and shredder for the credentials txt file.

Rules implemented here (docs/DESIGN.md §2):

* the file is opened with ``O_NOFOLLOW`` so a symlink is refused;
* a file that is not owned by the current user is refused;
* a world/group readable file or inbox is still imported, but it produces a
  warning;
* ``base_url`` must be https (only ``localhost`` / ``127.0.0.1`` / ``[::1]``
  may use http) and must not carry userinfo, a query string or a fragment;
* parse failures name the line number and the field, never the line content.

The file stays open while it is parsed so that the shredding step can rewrite
exactly the bytes that were read, through the very same file descriptor.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import CredentialError, CredentialParseError

MAX_FILE_BYTES = 64 * 1024

FIELDS = ('base_url', 'api_key', 'api_style')
API_STYLES = ('openai-compatible', 'anthropic', 'chat-completions')
HTTP_ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}


@dataclass
class ParsedCredentials:
    base_url: str | None = None
    api_key: str = ''
    api_style: str | None = None
    warnings: list[str] = field(default_factory=list)


class SecureFile:
    """An opened credentials file that can be shredded through its own fd."""

    def __init__(self, path: Path, fd: int, info: os.stat_result, content: str,
                 writable: bool, warnings: list[str]):
        self.path = path
        self.fd = fd
        self.info = info
        self.content = content
        self.writable = writable
        self.warnings = warnings

    def __enter__(self) -> 'SecureFile':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def shred(self) -> tuple[bool, list[str]]:
        """Zero the file (same length) through our own fd, then unlink it.

        Returns ``(deleted, warnings)``. The path is only unlinked when it
        still refers to the inode we opened, so a swapped file is never
        deleted or overwritten.
        """
        warnings: list[str] = []
        write_fd: int | None = None
        borrowed = True  # True -> the fd belongs to us and must not be closed here
        if self.writable:
            try:
                info = os.fstat(self.fd)
            except OSError:
                info = None
            if info is None or not self._safe_to_overwrite(info):
                warnings.append('凭据文件的属主或链接数在读取后发生变化，未能覆盖原始内容。')
            else:
                write_fd = self.fd
        else:
            candidate = None
            try:
                candidate = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW)
            except OSError:
                candidate = None
            if candidate is None:
                warnings.append('凭据文件不可写，未能覆盖原始内容。')
            else:
                try:
                    info = os.fstat(candidate)
                except OSError:
                    info = None
                if info is None or (info.st_dev, info.st_ino) != (self.info.st_dev, self.info.st_ino):
                    warnings.append('凭据文件在读取后被替换，未能覆盖原始内容。')
                    os.close(candidate)
                elif not self._safe_to_overwrite(info):
                    warnings.append('凭据文件的属主或链接数在读取后发生变化，未能覆盖原始内容。')
                    os.close(candidate)
                else:
                    write_fd = candidate
                    borrowed = False
        if write_fd is not None:
            try:
                _overwrite_fd(write_fd, self.info.st_size)
            except OSError:
                warnings.append('覆盖凭据文件失败。')
            finally:
                # A descriptor we opened ourselves is closed on every path.
                if not borrowed:
                    try:
                        os.close(write_fd)
                    except OSError:
                        pass
        deleted = False
        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            current = None
        except OSError:
            current = None
        if current is None:
            deleted = True
        elif (current.st_dev, current.st_ino) == (self.info.st_dev, self.info.st_ino):
            try:
                os.unlink(self.path)
                deleted = True
            except OSError:
                warnings.append('删除凭据文件失败。')
        else:
            warnings.append('凭据文件路径已被替换，未删除新文件。')
        return deleted, warnings

    @staticmethod
    def _safe_to_overwrite(info: os.stat_result) -> bool:
        return (info.st_uid == os.getuid()
                and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1)


def _overwrite_fd(fd: int, size: int) -> None:
    """Write exactly ``size`` zero bytes at the start of ``fd``."""
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = size
    block = b'\0' * 65536
    while remaining > 0:
        chunk = block[:min(remaining, len(block))]
        written = os.write(fd, chunk)
        if written <= 0:
            raise OSError('short write')
        remaining -= written
    os.ftruncate(fd, size)
    os.fsync(fd)


def open_credentials_file(path: Path) -> SecureFile:
    """Open the credentials file securely and read it.

    Raises :class:`CredentialError` for symlinks, non-regular files and files
    owned by somebody else.
    """
    warnings: list[str] = []
    fd = None
    writable = False
    last_error: OSError | None = None
    for flags, can_write in ((os.O_RDWR, True), (os.O_RDONLY, False)):
        try:
            fd = os.open(path, flags | os.O_NOFOLLOW)
            writable = can_write
            break
        except FileNotFoundError:
            raise CredentialError(
                f'找不到待导入的文件：{path}；请先调用 setup_begin 并让用户填写。') from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # the path is a symlink
                raise CredentialError('拒绝导入符号链接：凭据文件不能是软链接。') from None
            if exc.errno == errno.EISDIR:
                raise CredentialError('凭据路径是一个目录，不是文件。') from None
            last_error = exc
    if fd is None:
        raise CredentialError(
            f'无法读取凭据文件（{type(last_error).__name__}）') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError('凭据文件不是普通文件，拒绝导入。')
        if info.st_uid != os.getuid():
            raise CredentialError('凭据文件不属于当前用户，拒绝导入。')
        if info.st_size > MAX_FILE_BYTES:
            raise CredentialError('凭据文件过大，拒绝导入。')
        if info.st_mode & 0o077:
            warnings.append(
                '凭据文件权限过宽（其他用户可读）；建议执行 chmod 600 后重新导入。')
        content = _read_all(fd)
    except BaseException:
        os.close(fd)
        raise
    try:
        if path.parent.stat().st_mode & 0o077:
            warnings.append('inbox 目录权限过宽（其他用户可进入）；建议 chmod 700。')
    except OSError:
        pass
    return SecureFile(path, fd, info, content, writable, warnings)


def _read_all(fd: int) -> str:
    chunks: list[bytes] = []
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while total <= MAX_FILE_BYTES:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b''.join(chunks)
    if len(raw) > MAX_FILE_BYTES:
        raise CredentialError('凭据文件过大，拒绝导入。')
    return raw.decode('utf-8', errors='replace')


def parse_credentials(content: str) -> ParsedCredentials:
    """Parse ``key = value`` lines. Comment lines start with ``#``."""
    parsed = ParsedCredentials()
    seen: dict[str, int] = {}
    for number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise CredentialParseError(f'第 {number} 行格式错误：应为 key = value。')
        key, _, value = line.partition('=')
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if key not in FIELDS:
            # The "field name" may itself be a pasted key, so it is never echoed.
            raise CredentialParseError(f'第 {number} 行出现未知字段。')
        if key in seen:
            parsed.warnings.append(
                f'字段 {key} 出现多次（第 {seen[key]} 行与第 {number} 行），使用最后一次的值。')
        seen[key] = number
        if key == 'base_url':
            parsed.base_url = _validate_base_url(value, number)
        elif key == 'api_key':
            parsed.api_key = _validate_api_key(value, number)
        else:
            parsed.api_style = _validate_api_style(value, number)
    if not parsed.api_key:
        raise CredentialParseError('未找到 api_key，请在模板中填写后再导入。')
    return parsed


def _validate_api_key(value: str, number: int) -> str:
    if not value:
        raise CredentialParseError(f'第 {number} 行的 api_key 是空的。')
    if any(ch.isspace() for ch in value):
        raise CredentialParseError(f'第 {number} 行的 api_key 不能包含空白字符。')
    return value


def _validate_base_url(value: str, number: int) -> str | None:
    if not value:
        return None
    url = value.rstrip('/')
    try:
        parts = urlsplit(url)
    except ValueError:
        raise CredentialParseError(f'第 {number} 行的 base_url 无法解析。') from None
    if parts.scheme not in {'http', 'https'}:
        raise CredentialParseError(
            f'第 {number} 行的 base_url 必须使用 https（仅 localhost / 127.0.0.1 / [::1] 可用 http）。')
    host = (parts.hostname or '').lower()
    if not host:
        raise CredentialParseError(f'第 {number} 行的 base_url 缺少主机名。')
    if parts.scheme == 'http' and host not in HTTP_ALLOWED_HOSTS:
        raise CredentialParseError(
            f'第 {number} 行的 base_url 必须使用 https（仅 localhost / 127.0.0.1 / [::1] 可用 http）。')
    if parts.username or parts.password:
        raise CredentialParseError(f'第 {number} 行的 base_url 不能包含用户名或密码。')
    if parts.query:
        raise CredentialParseError(f'第 {number} 行的 base_url 不能包含查询参数。')
    if parts.fragment:
        raise CredentialParseError(f'第 {number} 行的 base_url 不能包含 # 片段。')
    return url


def _validate_api_style(value: str, number: int) -> str | None:
    if not value:
        return None
    style = value.strip().lower()
    if style not in API_STYLES:
        raise CredentialParseError(
            f'第 {number} 行的 api_style 只支持 openai-compatible / anthropic / chat-completions。')
    return style


def detect_api_style(base_url: str | None) -> str:
    """Auto-detect the provider style from the base url (docs/DESIGN.md §3)."""
    from urllib.parse import urlsplit
    host = (urlsplit(base_url or '').hostname or '').lower()
    if host == 'anthropic.com' or host.endswith('.anthropic.com'):
        return 'anthropic'
    if host == 'api.openai.com':
        return 'openai-compatible'
    return 'chat-completions'


def assert_no_secret_in_metadata(secret: str, *values: str | None) -> None:
    """Refuse to hand back metadata that embeds the secret itself."""
    if not secret:
        return
    for value in values:
        if value and secret in value:
            raise CredentialParseError(
                '模板内容有误：api_key 出现在 base_url 或 profile 里，请重新填写。')
