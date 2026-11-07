"""Provider configuration delegates all safety checks to the common command adapter."""
from .command import CommandExecutor


class CustomCommandExecutor(CommandExecutor):
    kind='custom-command'
    verification='experimental-unverified'
    def __init__(self, **kwargs):
        super().__init__(self.kind, **kwargs)
