"""Credential import for the said-done automation MCP server.

Public API (docs/DESIGN.md §10)::

    begin_import(settings, profile='default') -> dict
    import_credentials(settings, profile='default') -> dict
    credential_status(settings, profile='default') -> dict
    delete_credentials(settings, profile='default') -> dict
    get_api_key(settings, profile='default') -> pydantic.SecretStr | None
"""
from __future__ import annotations

from .errors import CredentialError, CredentialParseError
from .paths import (ENV_BACKEND, FILE_BACKEND, KEYRING_BACKEND, KEYRING_SERVICE)
from .service import (IMPORT_RESULT_KEYS, begin_import, credential_status,
                      delete_credentials, get_api_key, import_credentials)

__all__ = [
    'begin_import',
    'import_credentials',
    'credential_status',
    'delete_credentials',
    'get_api_key',
    'CredentialError',
    'CredentialParseError',
    'IMPORT_RESULT_KEYS',
    'ENV_BACKEND',
    'FILE_BACKEND',
    'KEYRING_BACKEND',
    'KEYRING_SERVICE',
]
