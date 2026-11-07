"""Provider configuration delegates all safety checks to the common command adapter."""
from .command import CommandExecutor


class OpenCodeExecutor(CommandExecutor):
    kind='opencode'
    def __init__(self, **kwargs):
        super().__init__(self.kind, **kwargs)
