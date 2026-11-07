"""Release metadata and checkout/installed setup-guide lookup."""
from importlib.resources import files
from pathlib import Path
import ast
import tomllib

from said_done.mcp_server import services


def test_packaged_guide_matches_checkout():
    root = Path(__file__).resolve().parents[1]
    packaged = files('said_done').joinpath('docs/AGENT_SETUP.md').read_text('utf-8')
    assert packaged == (root / 'docs/AGENT_SETUP.md').read_text('utf-8')
    assert services.get_setup_guide()['guide'] == packaged


def test_guide_without_checkout(monkeypatch, tmp_path):
    # Simulate a wheel's site-packages layout without a repository docs tree.
    monkeypatch.setattr(services, '__file__', str(tmp_path / 'said_done/mcp_server/services.py'))
    assert services.get_setup_guide()['guide'] == files('said_done').joinpath('docs/AGENT_SETUP.md').read_text('utf-8')


def test_release_metadata_and_python311_syntax():
    root = Path(__file__).resolve().parents[1]
    meta = tomllib.loads((root / 'pyproject.toml').read_text())
    assert meta['project']['name'] == 'said-done'
    assert meta['project']['version'] == '0.2.0'
    assert set(meta['project']['scripts']) == {'said-done', 'said-done-mcp'}
    for directory in ('said_done', 'tests'):
        for path in (root / directory).rglob('*.py'):
            ast.parse(path.read_text('utf-8'), filename=str(path), feature_version=(3, 11))
