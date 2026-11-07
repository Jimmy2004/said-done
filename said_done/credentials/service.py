"""Public credential workflow (docs/DESIGN.md §2 and §10).

The functions here are the contract used by the MCP server:

``begin_import``         create the template, return its path
``import_credentials``   store the key, shred the template, return a safe summary
``credential_status``    existence / backend / fingerprint only
``delete_credentials``   remove the stored secret
``get_api_key``          the secret as a ``pydantic.SecretStr`` (or ``None``)

No return value, exception or log line produced by this module contains the
key or the raw file content.
"""
from __future__ import annotations

import os

from pydantic import SecretStr

from .parsing import (assert_no_secret_in_metadata, open_credentials_file,
                      parse_credentials)
from .paths import (ENV_BACKEND, FILE_BACKEND, env_var_name,
                    import_path, secret_path, validate_profile)
from .storage import (LEGACY_MINIMAX_ENV, delete_stored_secret, fingerprint, lookup_secret,
                      store_secret)
from .template import write_template

#: Return keys of ``import_credentials`` (docs/DESIGN.md §2) plus the
#: ``updated_classifier`` patch required by docs/DESIGN.md §10.
IMPORT_RESULT_KEYS = (
    'stored', 'profile', 'backend', 'key_fingerprint', 'key_length',
    'base_url', 'api_style', 'file_deleted', 'warnings', 'updated_classifier',
    'supported_styles', 'detected', 'auth', 'probes', 'needs_api_style',
)

_OVERWRITE_WARNING = ('原文件已删除；删除前尽力用同长度零字节覆盖，但在 APFS/SSD 上覆盖只是尽力而为，'
                       '无法保证原始内容不可恢复。')


def begin_import(settings=None, profile: str = 'default') -> dict:
    """Create the fill-in template and return **only** its path.

    The agent must hand the path to the user and never read the file itself.
    """
    profile = validate_profile(profile)
    path, _created, _warnings = write_template(profile)
    return {'path': str(path)}


def import_credentials(settings=None, profile: str = 'default', *, client=None, transport=None) -> dict:
    """Read the template, store the key, shred the file.

    The function never writes ``config.json``: the returned
    ``updated_classifier`` is a merge-patch for the ``classifier`` section that
    the caller applies with ``patch_section`` + ``save_settings``.
    """
    profile = validate_profile(profile)
    path = import_path(profile)
    with open_credentials_file(path) as handle:
        warnings = list(handle.warnings)
        parsed = parse_credentials(handle.content)
        warnings.extend(parsed.warnings)

        from said_done.providers.probe import probe_api
        import httpx
        base_url = parsed.base_url
        assert_no_secret_in_metadata(parsed.api_key, base_url, profile)
        if client is not None and transport is not None:
            raise ValueError('pass client or transport, not both')
        if transport is not None:
            with httpx.Client(transport=transport) as probe_client:
                probe = probe_api(base_url, parsed.api_key, client=probe_client)
        else:
            probe = probe_api(base_url, parsed.api_key, client=client)
        api_style = parsed.api_style or probe['detected']
        warnings.extend(probe.get('warnings', []))
        if parsed.api_style and parsed.api_style not in probe['supported_styles']:
            warnings.append(f'api_style 指定为 {parsed.api_style}，但探测未确认该接口可用；以你填写的 api_style 为准。')
        if not api_style:
            warnings.append('未探测到可用接口；请填写 api_style 或检查 base_url/key。')
        if probe['detected'] == 'anthropic' and not parsed.api_style:
            base_url = probe['anthropic_base_url']

        backend, store_warnings = store_secret(profile, parsed.api_key)
        warnings.extend(store_warnings)

        file_deleted, shred_warnings = handle.shred()
        warnings.extend(shred_warnings)
        if file_deleted:
            warnings.append(_OVERWRITE_WARNING)

    # Clear a previous provider's endpoint when the template leaves this blank.
    updated_classifier: dict[str, str | None] = {
        'profile': profile, 'base_url': base_url,
    }

    if api_style:
        updated_classifier['kind'] = api_style

    return {
        **{field: probe[field] for field in ('supported_styles', 'detected', 'auth', 'probes')},
        'needs_api_style': api_style is None,
        'stored': True,
        'profile': profile,
        'backend': backend,
        'key_fingerprint': fingerprint(parsed.api_key),
        'key_length': len(parsed.api_key),
        'base_url': base_url,
        'api_style': api_style,
        'file_deleted': file_deleted,
        'warnings': warnings,
        'updated_classifier': updated_classifier,
    }


def credential_status(settings=None, profile: str = 'default') -> dict:
    """Report existence, backend and fingerprint -- never the secret."""
    profile = validate_profile(profile)
    secret, backend, env_name = lookup_secret(profile, allow_legacy_minimax=_is_minimax(settings))
    result: dict[str, object] = {
        'profile': profile,
        'exists': secret is not None,
        'backend': backend,
        'key_fingerprint': fingerprint(secret) if secret is not None else None,
        'warnings': [],
    }
    if env_name:
        result['env_var'] = env_name
    if backend == FILE_BACKEND:
        result['file_path'] = str(secret_path(profile))
        result['warnings'].append(
            '凭据保存在未加密文件里（权限 0600）；文件所有者与备份工具可以读取它。')
    if backend == ENV_BACKEND:
        result['warnings'].append(
            '当前生效的凭据来自环境变量，它不会出现在钥匙串或 secrets/ 目录里。')
    return result


def delete_credentials(settings=None, profile: str = 'default') -> dict:
    """Remove the stored secret for ``profile``."""
    profile = validate_profile(profile)
    removed, warnings = delete_stored_secret(profile)
    env_name = env_var_name(profile)
    if os.getenv(env_name):
        warnings.append(
            f'环境变量 {env_name} 仍然生效；如果不再需要，请自己在运行环境里清理它。')
    if _is_minimax(settings) and os.getenv(LEGACY_MINIMAX_ENV):
        warnings.append(
            f'环境变量 {LEGACY_MINIMAX_ENV} 仍然生效；如果不再需要，请自己在运行环境里清理它。')
    return {
        'profile': profile,
        'deleted': bool(removed),
        'backends': removed,
        'warnings': warnings,
    }


def get_api_key(settings=None, profile: str = 'default') -> SecretStr | None:
    """Return the secret for ``profile`` as a ``SecretStr`` (or ``None``)."""
    profile = validate_profile(profile)
    secret, _, _ = lookup_secret(profile, allow_legacy_minimax=_is_minimax(settings))
    if secret is None:
        return None
    return SecretStr(secret)


def _is_minimax(settings) -> bool:
    classifier = getattr(settings, 'classifier', None)
    return getattr(classifier, 'kind', None) == 'minimax'
