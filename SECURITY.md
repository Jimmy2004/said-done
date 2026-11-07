# Security

Report vulnerabilities privately through the repository's Security → Report a
vulnerability feature: https://github.com/Jimmy2004/said-done/security/advisories/new.
If private reporting is unavailable, contact Jimmy2004 privately through the
GitHub profile before sharing details. Do not post credentials, transcripts or
exploit details in public issues. Include the version, affected configuration,
impact and minimal reproduction with secrets removed.

Credentials are separate from configuration. The local credential importer
prefers the OS keyring (service `said-done`, username = profile). If unavailable,
it uses an explicitly reported **unencrypted** fallback file with mode 0600 in
the private configuration directory (0700). `SAID_DONE_API_KEY_<PROFILE>` can
supply CI secrets. Status responses show fingerprints rather than raw keys;
never put keys in MCP host config, prompts, logs, issues or committed files.

Screening-only mode is the default. Review classifications before enabling
execution. Transcript sources and installed plugins are trusted code; transcript
text itself is untrusted input. Execution isolation depends on the selected
provider: command-based providers do not all offer an OS sandbox. Review the
provider status and access controls in the setup guide before granting access.
