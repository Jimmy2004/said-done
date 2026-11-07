"""Provider configuration delegates all safety checks to the common command adapter."""
from .command import CommandExecutor


class ClaudeCodeExecutor(CommandExecutor):
    kind='claude-code'
    def __init__(self, **kwargs):
        super().__init__(self.kind, **kwargs)
