"""Key storage: system keychain first, 0600 file as fallback (docs/DESIGN.md §2).

The keychain entry uses ``service="said-done"`` and
``username="<profile>"``. When no keychain backend is usable the secret is
written to ``secrets/<profile>.key`` with mode 0600 and the caller reports
``backend="file-0600-unencrypted"`` plus a warning.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import stat
import tempfile
from pathlib import Path

import keyring

from .errors import CredentialError
from .paths import (ENV_BACKEND, FILE_BACKEND, KEYRING_BACKEND, KEYRING_SERVICE,
                    ensure_private_dir, env_var_name, secret_path, secrets_dir)

#: Maximum size of the unencrypted fallback file.
MAX_SECRET_BYTES = 64 * 1024

#: Legacy environment variable, still honoured for the minimax classifier.
LEGACY_MINIMAX_ENV = 'MINIMAX_API_KEY'


def fingerprint(secret: str) -> str:
    """``sha256:<first 8 hex chars>`` -- safe to show to a human."""
    return 'sha256:' + hashlib.sha256(secret.encode('utf-8')).hexdigest()[:8]


def keyring_backend():
    """Return a usable keyring backend, or ``None`` when there is none.

    Two backends must never be used: ``fail`` (every call raises) and
    ``null`` (every call silently succeeds and discards the secret, which
    would report a stored key that exists nowhere). A backend whose priority
    is zero or lower is treated as unusable as well.
    """
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if backend is None:
        return None
    if _is_unusable_backend(backend):
        return None
    return backend


def _is_unusable_backend(backend) -> bool:
    for name in ('fail', 'null'):
        module = _backend_module(name)
        if module is not None and isinstance(backend, module.Keyring):
            return True
    priority = getattr(backend, 'priority', None)
    if isinstance(priority, (int, float)) and priority <= 0:
        return True
    return False


def _backend_module(name: str):
    try:
        return importlib.import_module(f'keyring.backends.{name}')
    except Exception:
        return None


def store_secret(profile: str, secret: str) -> tuple[str, list[str]]:
    """Store ``secret`` for ``profile``. Returns ``(backend, warnings)``."""
    warnings: list[str] = []
    backend = keyring_backend()
    if backend is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, profile, secret)
        except Exception as exc:
            warnings.append(
                f'写入系统钥匙串失败（{type(exc).__name__}），已改用未加密文件保存。')
        else:
            warnings.extend(_drop_stale_file(profile))
            return KEYRING_BACKEND, warnings
    else:
        warnings.append('当前系统没有可用的钥匙串后端，已改用未加密文件保存。')
    try:
        write_secret_file(profile, secret)
    except OSError as exc:
        raise CredentialError(
            f'无法保存凭据（{type(exc).__name__}）；原始文件已保留，请不要删除。') from None
    warnings.append(
        'API key 以未加密文件形式保存在 secrets/ 目录（权限 0600）；'
        '文件所有者可读取它，备份工具也可能收录它。')
    return FILE_BACKEND, warnings


def _drop_stale_file(profile: str) -> list[str]:
    """Remove a leftover unencrypted file after the keychain write succeeded."""
    path = secret_path(profile)
    try:
        if path.exists():
            path.unlink()
            return ['已删除此前遗留的未加密密钥文件。']
    except OSError:
        raise CredentialError('无法移除旧的回退凭据文件；原始导入文件已保留。') from None
    return []


def write_secret_file(profile: str, secret: str) -> Path:
    """Write the fallback secret file atomically with mode 0600."""
    directory = ensure_private_dir(secrets_dir())
    path = secret_path(profile)
    fd, tmp = tempfile.mkstemp(prefix='.key-', dir=directory)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_secret_file(profile: str) -> str | None:
    """Read the unencrypted fallback file safely.

    Symlinks, directories, files owned by somebody else and oversized files are
    all ignored; a decode error never raises.
    """
    path = secret_path(profile)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_uid != os.getuid():
            return None
        if info.st_size > MAX_SECRET_BYTES:
            return None
        raw = os.read(fd, MAX_SECRET_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_SECRET_BYTES:
        return None
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None
    content = content.strip()
    return content or None


def env_secret(profile: str, *, allow_legacy_minimax: bool = False) -> tuple[str | None, str | None]:
    """Return ``(secret, env_var_name)`` from the environment, if set."""
    name = env_var_name(profile)
    value = os.getenv(name)
    if value and value.strip():
        return value.strip(), name
    if allow_legacy_minimax:
        value = os.getenv(LEGACY_MINIMAX_ENV)
        if value and value.strip():
            return value.strip(), LEGACY_MINIMAX_ENV
    return None, None


def delete_stored_secret(profile: str) -> tuple[list[str], list[str]]:
    """Remove a profile from keychain and fallback file.

    Returns ``(removed_backends, warnings)``.
    """
    removed: list[str] = []
    warnings: list[str] = []
    if keyring_backend() is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, profile)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            warnings.append(f'删除钥匙串条目失败（{type(exc).__name__}）。')
        else:
            removed.append(KEYRING_BACKEND)
    path = secret_path(profile)
    try:
        if path.exists():
            path.unlink()
            removed.append(FILE_BACKEND)
    except OSError as exc:
        warnings.append(f'删除未加密密钥文件失败（{type(exc).__name__}）。')
    return removed, warnings


def lookup_secret(profile: str, *, allow_legacy_minimax: bool = False) -> tuple[str | None, str | None, str | None]:
    """Find the secret for ``profile``.

    Returns ``(secret, backend, env_var_name)``; ``secret`` is ``None`` when
    nothing is stored. Environment variables win over stored secrets so CI can
    override them (docs/DESIGN.md §2).
    """
    secret, name = env_secret(profile, allow_legacy_minimax=allow_legacy_minimax)
    if secret is not None:
        return secret, ENV_BACKEND, name
    # A fallback can be newer than a still-readable keyring entry after a
    # failed keyring update. Successful keyring imports remove this file.
    value = read_secret_file(profile)
    if value:
        return value, FILE_BACKEND, None
    if keyring_backend() is not None:
        try:
            value = keyring.get_password(KEYRING_SERVICE, profile)
        except Exception:
            value = None
        if value:
            return value, KEYRING_BACKEND, None
    return None, None, None
