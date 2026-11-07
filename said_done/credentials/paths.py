"""Filesystem layout for the credential workflow (docs/DESIGN.md §1).

The config home (``$SAID_DONE_HOME``, default ``~/.config/said-done``) holds
``inbox/`` for the template the user fills in and ``secrets/`` for the
unencrypted fallback store used when no system keychain is available.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from said_done.config import config_home

from .errors import CredentialError

INBOX_DIRNAME = 'inbox'
SECRETS_DIRNAME = 'secrets'

#: keyring service name used for every stored profile (docs/DESIGN.md §2).
KEYRING_SERVICE = 'said-done'

#: Backend identifiers reported to callers.
KEYRING_BACKEND = 'keyring'
FILE_BACKEND = 'file-0600-unencrypted'
ENV_BACKEND = 'env'


_PROFILE_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def validate_profile(profile: str) -> str:
    """Reject anything that could escape the inbox/secrets directory."""
    if not isinstance(profile, str) or not _PROFILE_PATTERN.match(profile):
        raise CredentialError(
            'profile 只能包含字母、数字、下划线、点和连字符，长度 1-64')
    if profile in {'.', '..'}:
        raise CredentialError('profile 不能是 "." 或 ".."')
    return profile


def inbox_dir() -> Path:
    return config_home() / INBOX_DIRNAME


def secrets_dir() -> Path:
    return config_home() / SECRETS_DIRNAME


def import_path(profile: str) -> Path:
    return inbox_dir() / f'credentials-{validate_profile(profile)}.txt'


def secret_path(profile: str) -> Path:
    return secrets_dir() / f'{validate_profile(profile)}.key'


def env_var_name(profile: str) -> str:
    """``SAID_DONE_API_KEY_<PROFILE>`` with the profile upper-cased (docs/DESIGN.md §2).

    The profile is upper-cased verbatim: ``foo-bar`` maps to
    ``SAID_DONE_API_KEY_FOO-BAR``. Sanitising the name would let two different
    profiles collide on one variable.
    """
    return f'SAID_DONE_API_KEY_{profile.upper()}'


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with 0700 permissions (best effort on the chmod)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
