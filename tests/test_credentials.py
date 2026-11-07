"""Credential import tests.

No test touches the real macOS keychain and no test performs network I/O:
the keyring backend is replaced by an in-memory fake for every test, and the
config home is redirected to a temporary directory.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import keyring
import httpx
import pytest

from said_done.config import Settings
from said_done.credentials import (CredentialError, CredentialParseError, IMPORT_RESULT_KEYS,
                             begin_import, credential_status, delete_credentials, get_api_key,
                             import_credentials as real_import_credentials)
from said_done.credentials.paths import env_var_name, import_path, secret_path
from said_done.credentials.storage import read_secret_file

SECRET = 'sk-test-0123456789abcdefXYZ'
BASE_URL = 'https://api.openai.com/v1'


def response_transport(style='openai-compatible', status=400):
    def handle(request):
        endpoint = {'openai-compatible': '/responses', 'chat-completions': '/chat/completions',
                    'anthropic': '/messages'}[style]
        if status == 401 or (request.method == 'POST' and request.url.path == '/v1' + endpoint):
            return httpx.Response(status, json={'error': {'type': 'invalid_request_error'}})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def import_credentials(*args, **kwargs):
    kwargs.setdefault('transport', response_transport())
    return real_import_credentials(*args, **kwargs)


class FakeKeyring(keyring.backend.KeyringBackend):
    """In-memory keychain; the real one is never called."""

    priority = 99

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise keyring.errors.PasswordDeleteError('no such entry')
        del self.entries[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring():
    """Every test runs against the in-memory backend only."""
    previous = keyring.get_keyring()
    backend = FakeKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Redirect $SAID_DONE_HOME so inbox/ and secrets/ stay in the temp dir."""
    root = tmp_path / 'home'
    root.mkdir()
    monkeypatch.setenv('SAID_DONE_HOME', str(root))
    for name in list(os.environ):
        if name.startswith('SAID_DONE_API_KEY_') or name == 'MINIMAX_API_KEY':
            monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def settings():
    return Settings()


def fill(profile='default', base_url=BASE_URL, api_key=SECRET, api_style=None):
    """Create the template and fill it in like a user would."""
    begin_import(settings=None, profile=profile)
    path = import_path(profile)
    lines = [f'base_url = {base_url}', f'api_key = {api_key}']
    if api_style is not None:
        lines.append(f'api_style = {api_style}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def fingerprint(secret: str) -> str:
    return 'sha256:' + hashlib.sha256(secret.encode('utf-8')).hexdigest()[:8]


# --------------------------------------------------------------------------
# begin_import
# --------------------------------------------------------------------------

def test_begin_import_returns_only_the_path(settings, home):
    result = begin_import(settings)
    assert list(result) == ['path']
    assert result['path'] == str(home / 'inbox' / 'credentials-default.txt')


def test_begin_import_creates_a_private_template(settings, home):
    path = Path(begin_import(settings)['path'])
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    text = path.read_text('utf-8')
    assert 'base_url =' in text and 'api_key =' in text and 'api_style =' in text
    assert SECRET not in text


def test_begin_import_never_clobbers_an_existing_file(settings):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('api_key = already-there\n', encoding='utf-8')
    begin_import(settings)
    assert path.read_text('utf-8') == 'api_key = already-there\n'


def test_profile_cannot_escape_the_inbox(settings):
    for bad in ('../escape', 'a/b', 'x' * 65, ''):
        with pytest.raises(CredentialError):
            begin_import(settings, bad)


# --------------------------------------------------------------------------
# import_credentials
# --------------------------------------------------------------------------

def test_import_stores_the_key_and_deletes_the_file(settings, fake_keyring):
    path = fill()
    result = import_credentials(settings)

    assert set(result) == set(IMPORT_RESULT_KEYS)
    assert result['stored'] is True
    assert result['backend'] == 'keyring'
    assert result['key_fingerprint'] == fingerprint(SECRET)
    assert result['key_length'] == len(SECRET)
    assert result['base_url'] == BASE_URL
    assert result['api_style'] == 'openai-compatible'
    assert result['file_deleted'] is True
    assert not path.exists()
    assert fake_keyring.entries[('said-done', 'default')] == SECRET


def test_import_returns_a_classifier_patch(settings):
    fill()
    result = import_credentials(settings)
    assert result['updated_classifier'] == {
        'profile': 'default', 'kind': 'openai-compatible', 'base_url': BASE_URL}


def test_import_does_not_write_the_config_file(settings, home):
    config = home / 'config.json'
    config.write_text('{"schema_version": 2}', encoding='utf-8')
    fill()
    import_credentials(settings)
    assert config.read_text('utf-8') == '{"schema_version": 2}'


def test_import_result_never_contains_the_key(settings):
    fill()
    result = import_credentials(settings)
    assert SECRET not in repr(result)


def test_overwrite_warning_states_the_right_thing(settings):
    fill()
    result = import_credentials(settings)
    joined = ' '.join(result['warnings'])
    assert '无法保证' in joined
    assert '未必能恢复' not in joined


def test_import_rejects_a_symlink(settings, home):
    target = home / 'elsewhere.txt'
    target.write_text(f'base_url = {BASE_URL}\napi_key = {SECRET}\n', encoding='utf-8')
    link = import_path('default')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    with pytest.raises(CredentialError) as exc:
        import_credentials(settings)
    assert '符号链接' in str(exc.value)
    assert SECRET not in str(exc.value)
    assert target.read_text('utf-8').endswith(f'api_key = {SECRET}\n')


def test_import_rejects_a_file_owned_by_somebody_else(settings, monkeypatch):
    fill()
    real_uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: real_uid + 1)
    with pytest.raises(CredentialError) as exc:
        import_credentials(settings)
    assert '不属于当前用户' in str(exc.value)
    assert SECRET not in str(exc.value)


def test_import_warns_but_still_imports_on_loose_permissions(settings):
    path = fill()
    path.chmod(0o644)
    result = import_credentials(settings)
    assert result['stored'] is True
    assert any('权限过宽' in w for w in result['warnings'])


def test_import_falls_back_to_a_0600_file_without_a_keychain(settings, home):
    keyring.set_keyring(keyring.backends.fail.Keyring())
    path = fill()
    result = import_credentials(settings)

    assert result['backend'] == 'file-0600-unencrypted'
    assert any('未加密' in w for w in result['warnings'])
    fallback = secret_path('default')
    assert fallback.exists()
    assert fallback.stat().st_mode & 0o777 == 0o600
    assert fallback.read_text('utf-8') == SECRET
    assert not path.exists()


def test_import_removes_a_stale_unencrypted_file_after_a_keychain_write(settings, home):
    fallback = secret_path('default')
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text('stale', encoding='utf-8')
    fill()
    import_credentials(settings)
    assert not fallback.exists()


# --------------------------------------------------------------------------
# parsing: safe errors, no echo of user content
# --------------------------------------------------------------------------

def test_unknown_field_is_never_echoed(settings):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'sk_pasted_key_abc = x\napi_key = {SECRET}\n', encoding='utf-8')
    with pytest.raises(CredentialParseError) as exc:
        import_credentials(settings)
    assert 'sk_pasted_key_abc' not in str(exc.value)
    assert SECRET not in str(exc.value)
    assert '第 1 行' in str(exc.value)


def test_missing_api_key_is_reported_without_content(settings):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'base_url = {BASE_URL}\n', encoding='utf-8')
    with pytest.raises(CredentialParseError) as exc:
        import_credentials(settings)
    assert 'api_key' in str(exc.value)
    assert BASE_URL not in str(exc.value)


@pytest.mark.parametrize('url', [
    'http://api.openai.com/v1',
    'https://user:pass@api.openai.com/v1',
    'https://api.openai.com/v1?token=abc',
    'https://api.openai.com/v1#frag',
    'ftp://api.openai.com/v1',
])
def test_base_url_must_be_plain_https(settings, url):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'base_url = {url}\napi_key = {SECRET}\n', encoding='utf-8')
    with pytest.raises(CredentialParseError) as exc:
        import_credentials(settings)
    assert 'base_url' in str(exc.value)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize('url', [
    'http://localhost:11434/v1',
    'http://127.0.0.1:8080/v1',
    'http://[::1]:8080/v1',
])
def test_loopback_base_url_may_use_http(settings, url):
    fill(base_url=url)
    result = import_credentials(settings)
    assert result['base_url'] == url


def test_api_key_inside_the_base_url_is_refused(settings):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'base_url = https://example.com/{SECRET}\napi_key = {SECRET}\n',
                    encoding='utf-8')
    with pytest.raises(CredentialParseError) as exc:
        import_credentials(settings)
    assert SECRET not in str(exc.value)


def test_api_key_inside_the_profile_is_refused(settings):
    begin_import(settings, 'sk-leaked')
    path = import_path('sk-leaked')
    path.write_text(f'base_url = {BASE_URL}\napi_key = sk-leaked\n', encoding='utf-8')
    with pytest.raises(CredentialParseError) as exc:
        import_credentials(settings, 'sk-leaked')
    assert 'sk-leaked' not in str(exc.value)


def test_api_key_with_whitespace_is_refused(settings):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'base_url = {BASE_URL}\napi_key = {SECRET} extra\n', encoding='utf-8')
    with pytest.raises(CredentialParseError):
        import_credentials(settings)


# --------------------------------------------------------------------------
# shredding
# --------------------------------------------------------------------------

def test_the_file_is_zeroed_before_it_is_unlinked(settings, monkeypatch):
    fill()
    seen: dict[str, bytes] = {}
    real_unlink = os.unlink

    def spy(path, *args, **kwargs):
        target = os.fspath(path)
        try:
            seen[target] = Path(target).read_bytes()
        except OSError:
            pass
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', spy)
    import_credentials(settings)
    content = seen[str(import_path('default'))]
    assert content == b'\0' * len(content)
    assert len(content) > 0


def test_a_replaced_file_is_neither_overwritten_nor_deleted(settings, home, monkeypatch):
    path = fill()

    def swap(profile, secret):
        replacement = home / 'replacement.txt'
        replacement.write_text('someone else\n', encoding='utf-8')
        os.replace(replacement, path)
        return 'keyring', []

    monkeypatch.setattr('said_done.credentials.service.store_secret', swap)
    result = import_credentials(settings)
    assert result['file_deleted'] is False
    assert path.exists()
    assert path.read_text('utf-8') == 'someone else\n'


def test_a_hardlinked_file_is_not_overwritten(settings, home):
    path = fill()
    twin = home / 'twin.txt'
    os.link(path, twin)
    result = import_credentials(settings)
    assert result['file_deleted'] is True
    assert twin.read_text('utf-8').endswith(f'api_key = {SECRET}\n')
    assert any('链接数' in w for w in result['warnings'])


def test_a_read_only_file_is_deleted_even_without_overwriting(settings):
    path = fill()
    path.chmod(0o400)
    result = import_credentials(settings)
    assert result['file_deleted'] is True
    assert not path.exists()


# --------------------------------------------------------------------------
# fallback file reads
# --------------------------------------------------------------------------

def test_read_secret_file_rejects_a_symlink(settings, home):
    target = home / 'target.key'
    target.write_text(SECRET, encoding='utf-8')
    link = secret_path('default')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    assert read_secret_file('default') is None


def test_read_secret_file_rejects_a_directory(settings, home):
    path = secret_path('default')
    path.mkdir(parents=True, exist_ok=True)
    assert read_secret_file('default') is None


def test_read_secret_file_rejects_a_foreign_owner(settings, monkeypatch):
    secret_path('default').parent.mkdir(parents=True, exist_ok=True)
    secret_path('default').write_text(SECRET, encoding='utf-8')
    real_uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: real_uid + 1)
    assert read_secret_file('default') is None


def test_read_secret_file_rejects_binary_content(settings):
    path = secret_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\xff\xfe\x00binary')
    assert read_secret_file('default') is None


# --------------------------------------------------------------------------
# status / delete / get_api_key
# --------------------------------------------------------------------------

def test_status_reports_backend_and_fingerprint_only(settings):
    fill()
    result = import_credentials(settings)
    status = credential_status(settings)
    assert status['exists'] is True
    assert status['backend'] == 'keyring'
    assert status['key_fingerprint'] == result['key_fingerprint']
    assert SECRET not in repr(status)


def test_status_when_nothing_is_stored(settings):
    status = credential_status(settings)
    assert status['exists'] is False
    assert status['backend'] is None
    assert status['key_fingerprint'] is None


def test_delete_removes_the_keychain_entry_and_the_fallback_file(settings, home, fake_keyring):
    fill()
    import_credentials(settings)
    fallback = secret_path('default')
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text(SECRET, encoding='utf-8')
    fallback.chmod(0o600)

    result = delete_credentials(settings)
    assert result['deleted'] is True
    assert 'keyring' in result['backends']
    assert 'file-0600-unencrypted' in result['backends']
    assert ('said-done', 'default') not in fake_keyring.entries
    assert not fallback.exists()


def test_get_api_key_returns_a_secret_str(settings):
    fill()
    import_credentials(settings)
    secret = get_api_key(settings)
    assert secret is not None
    assert secret.get_secret_value() == SECRET
    assert SECRET not in repr(secret)


def test_get_api_key_is_none_when_nothing_is_stored(settings):
    assert get_api_key(settings) is None


# --------------------------------------------------------------------------
# environment overrides
# --------------------------------------------------------------------------

def test_env_var_name_is_the_profile_upper_cased_verbatim():
    assert env_var_name('default') == 'SAID_DONE_API_KEY_DEFAULT'
    assert env_var_name('foo-bar') == 'SAID_DONE_API_KEY_FOO-BAR'
    assert env_var_name('foo_bar') == 'SAID_DONE_API_KEY_FOO_BAR'
    assert env_var_name('foo-bar') != env_var_name('foo_bar')


def test_env_var_overrides_the_stored_key(settings, monkeypatch):
    fill()
    import_credentials(settings)
    monkeypatch.setenv('SAID_DONE_API_KEY_DEFAULT', 'sk-from-env')
    secret = get_api_key(settings)
    assert secret is not None and secret.get_secret_value() == 'sk-from-env'
    status = credential_status(settings)
    assert status['backend'] == 'env'
    assert status['env_var'] == 'SAID_DONE_API_KEY_DEFAULT'
    assert status['key_fingerprint'] == fingerprint('sk-from-env')


def test_env_var_works_for_a_dashed_profile(settings, monkeypatch):
    monkeypatch.setenv('SAID_DONE_API_KEY_FOO-BAR', 'sk-dashed')
    secret = get_api_key(settings, 'foo-bar')
    assert secret is not None and secret.get_secret_value() == 'sk-dashed'


def test_delete_warns_while_an_env_var_is_still_active(settings, monkeypatch):
    monkeypatch.setenv('SAID_DONE_API_KEY_DEFAULT', 'sk-from-env')
    result = delete_credentials(settings)
    assert any('SAID_DONE_API_KEY_DEFAULT' in w for w in result['warnings'])
    assert 'sk-from-env' not in repr(result)


def test_legacy_minimax_env_var_is_used_for_the_minimax_classifier(settings, monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'sk-legacy')
    minimax = Settings(classifier={'kind': 'minimax'})
    secret = get_api_key(minimax)
    assert secret is not None and secret.get_secret_value() == 'sk-legacy'


# --------------------------------------------------------------------------
# provider style detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize('url,style', [
    ('https://api.anthropic.com/v1', 'anthropic'),
    ('https://api.minimaxi.com/v1', 'chat-completions'),
    ('https://api.openai.com/v1', 'openai-compatible'),
])
def test_api_style_is_detected_from_endpoint_responses(settings, url, style):
    fill(base_url=url)
    result = import_credentials(settings, transport=response_transport(style))
    assert result['api_style'] == style
    assert result['updated_classifier']['kind'] == style


def test_explicit_api_style_wins(settings):
    fill(base_url='https://api.openai.com/v1', api_style='openai-compatible')
    result = import_credentials(settings)
    assert result['api_style'] == 'openai-compatible'


def test_minimax_style_is_rejected(settings):
    fill(base_url='', api_key=SECRET, api_style='minimax')
    with pytest.raises(CredentialParseError, match='chat-completions'):
        import_credentials(settings)


def test_the_classifier_patch_applies_to_the_settings(settings):
    """The caller is expected to apply updated_classifier with patch_section."""
    from said_done.config import patch_section

    fill(base_url='https://api.anthropic.com/v1')
    result = import_credentials(settings, transport=response_transport('anthropic'))
    patched = patch_section(settings, 'classifier', result['updated_classifier'])
    assert patched.classifier.base_url == 'https://api.anthropic.com'
    assert patched.classifier.kind == 'anthropic'
    assert patched.classifier.profile == 'default'


# --------------------------------------------------------------------------
# leak sweep: a failure must never echo the secret
# --------------------------------------------------------------------------

@pytest.mark.parametrize('content', [
    '',
    'api_key',
    'api_key =',
    f'api_key = {SECRET}\napi_key =',
    f'unknown_{SECRET} = x',
    f'{SECRET} = x',
    'base_url = not a url\napi_key = ' + SECRET,
    f'base_url = https://u:p@h/v\napi_key = {SECRET}',
    f'base_url = https://h/v?k={SECRET}\napi_key = {SECRET}',
    f'base_url = https://h/v#{SECRET}\napi_key = {SECRET}',
    f'api_style = nope\napi_key = {SECRET}',
    f'api_key = {SECRET}\napi_key = {SECRET} trailing',
    'no equals sign here',
])
def test_no_failure_ever_echoes_the_key(settings, content):
    path = import_path('default')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    try:
        text = repr(import_credentials(settings))
    except CredentialError as exc:
        text = str(exc)
    assert SECRET not in text


def test_a_failing_keychain_falls_back_to_the_file(settings, home, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('keychain exploded: ' + SECRET)

    monkeypatch.setattr(keyring.get_keyring(), 'set_password', boom)
    fill()
    result = import_credentials(settings)
    assert result['backend'] == 'file-0600-unencrypted'
    assert any('RuntimeError' in w for w in result['warnings'])
    assert SECRET not in repr(result)
    assert secret_path('default').read_text('utf-8') == SECRET


def test_a_total_storage_failure_keeps_the_users_file(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError('disk on fire')

    def keychain_boom(*args, **kwargs):
        raise RuntimeError('keychain exploded')

    monkeypatch.setattr(keyring.get_keyring(), 'set_password', keychain_boom)
    monkeypatch.setattr('said_done.credentials.storage.write_secret_file', boom)
    path = fill()
    with pytest.raises(CredentialError) as exc:
        import_credentials(settings)
    assert SECRET not in str(exc.value)
    assert path.exists()
    assert SECRET in path.read_text('utf-8')


def test_shred_does_not_leak_the_reopened_descriptor(settings, home, monkeypatch):
    """A failing overwrite must still close the descriptor we opened."""
    import errno

    path = fill()
    real_open = os.open

    def fake_open(target, flags, *args, **kwargs):
        # Pretend the file cannot be opened for writing at first.
        if flags & os.O_CREAT == 0 and flags & os.O_RDWR == os.O_RDWR:
            raise PermissionError(errno.EACCES, 'read-only file')
        return real_open(target, flags, *args, **kwargs)

    def boom(fd, size):
        raise OSError('simulated overwrite failure')

    monkeypatch.setattr('said_done.credentials.parsing.os.open', fake_open)
    monkeypatch.setattr('said_done.credentials.parsing._overwrite_fd', boom)

    before = len(os.listdir('/dev/fd'))
    result = import_credentials(settings)
    after = len(os.listdir('/dev/fd'))

    assert after <= before, 'a file descriptor leaked'
    assert any('覆盖凭据文件失败' in w for w in result['warnings'])
    assert result['file_deleted'] is True
    assert not path.exists()


def test_the_delete_warning_never_claims_a_successful_overwrite(settings):
    path = fill()
    path.chmod(0o400)  # cannot be overwritten, only deleted
    result = import_credentials(settings)
    joined = ' '.join(result['warnings'])
    assert '已用同长度零字节覆盖' not in joined
    assert '尽力' in joined
    assert '无法保证' in joined
    assert any('未能覆盖' in w for w in result['warnings'])
    assert not path.exists()


def test_the_null_keychain_backend_is_not_usable(settings, home):
    """The null backend silently discards secrets, so we must fall back."""
    keyring.set_keyring(keyring.backends.null.Keyring())
    fill()
    result = import_credentials(settings)
    assert result['backend'] == 'file-0600-unencrypted'
    assert secret_path('default').read_text('utf-8') == SECRET
    status = credential_status(settings)
    assert status['backend'] == 'file-0600-unencrypted'
    assert status['key_fingerprint'] == fingerprint(SECRET)


def test_a_backend_with_priority_zero_is_not_usable(settings, home):
    class ZeroPriorityKeyring(keyring.backend.KeyringBackend):
        priority = 0

        def get_password(self, service, username):
            return None

        def set_password(self, service, username, password):
            pass

        def delete_password(self, service, username):
            raise keyring.errors.PasswordDeleteError('no such entry')

    keyring.set_keyring(ZeroPriorityKeyring())
    fill()
    result = import_credentials(settings)
    assert result['backend'] == 'file-0600-unencrypted'


def test_a_usable_backend_is_still_preferred(settings, fake_keyring):
    assert fake_keyring.priority > 0
    fill()
    result = import_credentials(settings)
    assert result['backend'] == 'keyring'


def test_blank_endpoint_clears_a_previous_providers_url(settings):
    from said_done.config import Settings, patch_section
    previous = Settings(classifier={'kind': 'openai-compatible', 'base_url': 'https://previous.example/v1'})
    fill(base_url='', api_style='anthropic')
    result = import_credentials(previous)
    patched = patch_section(previous, 'classifier', result['updated_classifier'])
    assert patched.classifier.kind == 'anthropic'
    assert patched.classifier.base_url is None


def test_fallback_rotation_wins_over_a_still_readable_old_key(settings, fake_keyring, monkeypatch):
    fill(api_key='old-credential-for-rotation')
    import_credentials(settings)
    def unavailable(*args):
        raise keyring.errors.KeyringError('unavailable')
    monkeypatch.setattr(fake_keyring, 'set_password', unavailable)
    fill(api_key=SECRET)
    result = import_credentials(settings)
    assert result['backend'] == 'file-0600-unencrypted'
    assert get_api_key(settings).get_secret_value() == SECRET
    assert credential_status(settings)['key_fingerprint'] == fingerprint(SECRET)


@pytest.mark.parametrize('explicit', [None, 'anthropic'])
def test_probe_failure_preserves_key_and_shreds(settings, fake_keyring, explicit):
    path = fill(api_style=explicit)
    result = import_credentials(settings, transport=response_transport(status=401))
    assert not path.exists() and result['file_deleted']
    assert get_api_key(settings).get_secret_value() == SECRET
    assert result['needs_api_style'] is (explicit is None)
    assert result['supported_styles'] == [] and result['detected'] is None
    if explicit:
        assert result['updated_classifier']['kind'] == explicit
        assert any('未确认' in w for w in result['warnings'])
    else:
        assert 'kind' not in result['updated_classifier']
        assert any('base_url/key' in w for w in result['warnings'])


def test_explicit_style_is_verified(settings):
    fill(api_style='openai-compatible')
    result = import_credentials(settings)
    assert len(result['probes']) == 6
    assert not any('未确认' in w for w in result['warnings'])


def test_blank_style_uses_probe_even_on_misleading_host(settings):
    fill(base_url='https://api.anthropic.com/v1')
    result = import_credentials(settings, transport=response_transport('chat-completions'))
    assert result['updated_classifier']['kind'] == 'chat-completions'


def test_import_accepts_client(settings):
    fill()
    with httpx.Client(transport=response_transport()) as client:
        result = real_import_credentials(settings, client=client)
        assert not client.is_closed
    assert result['detected'] == 'openai-compatible'
