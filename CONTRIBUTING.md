# Contributing

Use Python 3.11–3.13:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

Tests use local fakes; API accounts, keychains, external agent CLIs and network
access are not required. See [the design](docs/DESIGN.md) for architecture and
[agent setup](docs/AGENT_SETUP.md) for configuration. When changing the setup
guide, update `said_done/docs/AGENT_SETUP.md` to match the canonical docs copy.

To add a transcript source plugin, implement the `TranscriptSource` interface
in `said_done/ingestion/interface.py`. Accept `(source_id, options, repository)`
and expose a Pydantic `Options` model for setup schema discovery. Publish the
class under `[project.entry-points."said_done.sources"]` in your distribution.
See `said_done/ingestion/registry.py` and `tests/test_sources_plugins.py`.

To add an executor provider, implement the interface in
`said_done/executor/interface.py`, add its provider under
`said_done/executor/providers/`, and register its kind, discovery and construction
in `said_done/executor/registry.py` and configuration literals in
`said_done/config.py`. Add tests using fake processes; document sandbox and
credential requirements, and keep unverified support marked as such.
