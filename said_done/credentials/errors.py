"""Error types for the credential workflow.

Every message produced by this package is written so it can be handed back to a
host agent: it names the *field* and the *line number* that failed, never the
file content and never the secret itself (docs/DESIGN.md §2).

"Any return value, exception or log must not contain the key or the raw file
content" -- so the helpers below are the only sanctioned way to raise.
"""
from __future__ import annotations


class CredentialError(RuntimeError):
    """A credential operation could not be completed safely."""


class CredentialParseError(CredentialError):
    """The credentials txt file could not be parsed.

    The message identifies the line number and the field name only.
    """
