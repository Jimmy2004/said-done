"""CLI flags are assumptions; this adapter requires local user verification."""
from .command import CommandExecutor


class PiExecutor(CommandExecutor):
    kind='pi'
    verification='experimental-unverified'
    def __init__(self, **kwargs):
        super().__init__(self.kind, **kwargs)
