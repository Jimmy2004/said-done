"""Stdio MCP entry point. Importing this package starts no worker."""


def main() -> None:
    from .server import main as run
    run()
