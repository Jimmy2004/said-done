"""Stdout JSON-RPC client for an external cloud transcript MCP server.

Transport
---------
The source is an MCP server that speaks JSON-RPC 2.0 over stdio (one JSON
object per line).  The command that starts it is supplied by the caller:
there is no built-in default, no environment lookup and no path guessing,
so the deployment decides which binary, distribution or container runs the
server.

The server loads its own credentials inside its own environment.  This module
never reads, copies, logs or returns those credentials: it only speaks the
protocol and hands back the server's own payload.

Every :meth:`CloudTranscriptClient.fetch` call starts one server process, performs
the ``initialize`` / ``notifications/initialized`` handshake, issues exactly
one ``tools/call`` and shuts the process down again, so a wedged or crashed
server cannot leak into the next poll and no child is left behind.

Response shape::

    {"tool": "get_transcripts", "results": [...], "resultCount": 12,
     "truncated": false, "source": "remote"}

Transcript record shape::

    {"sourceType": "transcript", "id": 42, "timeMs": 1730000000000,
     "title": "...", "text": "...", "device": "phone"}

A transcript record carries a single timestamp, so a normalized segment
starts and ends at the same instant.  Record ids are only unique per
``(device, sourceType)``, therefore segment ids are namespaced by the
device identity exactly as the server stores it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any

from said_done.contracts import TranscriptSegment

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "cloud-transcript-client"
DEFAULT_TIMEOUT = 30.0
#: Seconds the child may take to exit on its own after stdin is closed.
SHUTDOWN_GRACE = 2.0
#: ``source`` value written on every normalized segment.
SOURCE = "cloud-transcript-sync"
#: ``TranscriptSegment.text`` upper bound; longer payloads are rejected.
MAX_TEXT = 100000

_EOF = object()


class CloudTranscriptError(RuntimeError):
    """Any failure of the cloud MCP transport."""


class CloudTranscriptTimeout(CloudTranscriptError):
    """The server did not answer within the configured timeout."""


def _split_call(args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Split a call request into ``(tool name, tool arguments)``.

    Both shapes are accepted::

        {"tool": "get_transcripts", "arguments": {"sinceId": 0}}
        {"tool": "get_transcripts", "sinceId": 0}

    ``arguments`` wins over inline keys when both are present.
    """
    if not isinstance(args, Mapping):
        raise TypeError("fetch() expects a mapping")
    payload = dict(args)
    tool = payload.pop("tool", None)
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("fetch() requires a non-empty 'tool' name")
    nested = payload.pop("arguments", None)
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ValueError("'arguments' must be a mapping")
        payload = dict(nested)
    return tool.strip(), payload


class CloudTranscriptClient:
    """Client for a cloud transcript MCP server over stdio JSON-RPC.

    ``command`` is the explicit argv that starts the server.  It is
    mandatory: the client never guesses a binary, never reads a credential
    file and never falls back to a built-in default, so the transport is
    always exactly what the caller configured.
    """

    def __init__(
        self,
        command: Sequence[str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.command = _validate_command(command)
        self.timeout = float(timeout)

    # -- public API ---------------------------------------------------

    def fetch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Call one MCP tool and return its decoded response dictionary.

        ``args`` selects the tool and its arguments, see :func:`_split_call`.
        The returned dictionary is the server's own payload, e.g.
        ``{"tool": ..., "results": [...], "resultCount": ..., "truncated": ...}``.

        Upstream error text is never copied into the exception, because it may
        echo private payload or secrets; only the tool name and the failure
        category are reported.
        """
        tool, arguments = _split_call(args)
        result = self._call(tool, arguments)
        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise CloudTranscriptError(f"cloud tool {tool!r} returned no content")
        first = content[0]
        if not isinstance(first, Mapping) or not isinstance(first.get("text"), str):
            raise CloudTranscriptError(f"cloud tool {tool!r} returned malformed content")
        try:
            data = json.loads(first["text"])
        except json.JSONDecodeError:
            raise CloudTranscriptError(
                f"cloud tool {tool!r} returned an invalid payload"
            ) from None
        if not isinstance(data, dict):
            raise CloudTranscriptError(f"cloud tool {tool!r} returned a non-object payload")
        return data

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Convenience wrapper around :meth:`fetch`."""
        return self.fetch({"tool": tool, "arguments": dict(arguments or {})})

    # -- transport ----------------------------------------------------

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        process = self._spawn()
        readers: list[threading.Thread] = []
        try:
            stdout, stderr = process.stdout, process.stderr
            if stdout is None or stderr is None:
                raise CloudTranscriptError("cloud server has no output pipes")
            lines: Queue[Any] = Queue()
            stdout_reader = threading.Thread(
                target=_drain, args=(stdout, lines), daemon=True
            )
            stderr_reader = threading.Thread(target=_drain, args=(stderr, None), daemon=True)
            readers.extend((stdout_reader, stderr_reader))
            stdout_reader.start()
            stderr_reader.start()

            _write(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": PROTOCOL_VERSION,
                                        "capabilities": {},
                                        "clientInfo": {"name": CLIENT_NAME}}})
            _await(lines, deadline, 1, tool, self.timeout)

            _write(process, {"jsonrpc": "2.0", "method": "notifications/initialized",
                             "params": {}})
            _write(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": tool, "arguments": dict(arguments)}})
            reply = _await(lines, deadline, 2, tool, self.timeout)

            if reply.get("error") is not None:
                raise CloudTranscriptError(f"cloud tool {tool!r} failed")
            result = reply.get("result")
            if not isinstance(result, Mapping):
                raise CloudTranscriptError(f"cloud tool {tool!r} returned no result")
            if result.get("isError"):
                raise CloudTranscriptError(f"cloud tool {tool!r} failed")
            return dict(result)
        finally:
            _shutdown(process, readers)

    def _spawn(self) -> subprocess.Popen:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return subprocess.Popen(  # noqa: S603 - explicit caller-supplied command
                list(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **kwargs,
            )
        except OSError:
            raise CloudTranscriptError("could not start the cloud server") from None


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    """Normalize the caller-supplied argv, refusing anything ambiguous."""
    if command is None or isinstance(command, (str, bytes)):
        raise ValueError(
            "an explicit command array is required to start the cloud server"
        )
    argv = tuple(command)
    if not argv:
        raise ValueError(
            "an explicit command array is required to start the cloud server"
        )
    for part in argv:
        if not isinstance(part, str) or not part.strip():
            raise ValueError("the command array must contain non-empty strings")
    return argv


def _drain(stream, queue: Queue[Any] | None) -> None:
    """Forward stdout lines to ``queue``; stderr is drained and discarded.

    stderr may carry diagnostics, never payload we want to keep, and it must
    be drained so the child never blocks on a full pipe.
    """
    try:
        for line in stream:
            if queue is not None:
                queue.put(line)
    except (OSError, ValueError):
        pass
    finally:
        if queue is not None:
            queue.put(_EOF)


def _write(process: subprocess.Popen, message: Mapping[str, Any]) -> None:
    stdin = process.stdin
    if stdin is None:
        raise CloudTranscriptError("cloud server has no stdin")
    try:
        stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        stdin.flush()
    except (OSError, ValueError):
        raise CloudTranscriptError("could not write to the cloud server") from None


def _await(
    lines: Queue[Any], deadline: float, wanted: int, tool: str, timeout: float
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CloudTranscriptTimeout(
                f"cloud tool {tool!r} timed out after {timeout:g}s"
            )
        try:
            line = lines.get(timeout=min(remaining, 0.5))
        except Empty:
            continue
        if line is _EOF:
            raise CloudTranscriptError("cloud server closed the connection")
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != wanted:
            continue
        return message


def _shutdown(process: subprocess.Popen, readers: list[threading.Thread]) -> None:
    """Close stdin, let the child exit, then force it down and reap it.

    The output streams are released only after the child is gone: closing a
    pipe while a reader thread is blocked inside ``read`` can block the
    closing thread itself and defeat the timeout.
    """
    stdin = process.stdin
    if stdin is not None:
        try:
            stdin.close()
        except (OSError, ValueError):
            pass
    try:
        if process.poll() is None:
            try:
                process.wait(timeout=SHUTDOWN_GRACE)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=SHUTDOWN_GRACE)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=SHUTDOWN_GRACE)
    except OSError:
        pass
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for thread in readers:
            thread.join(timeout=1.0)


# -- normalization ---------------------------------------------------


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"cloud record has an invalid {field}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"cloud record has an invalid {field}")


def normalize_transcript(
    record: Mapping[str, Any], device: str | None = None
) -> TranscriptSegment | None:
    """Map one cloud transcript record onto a ``TranscriptSegment``.

    ``record`` is one entry of the ``results`` list of a ``get_transcripts``
    or ``search_evidence`` response.  ``device`` is the device identity the
    caller polled with; when it is ``None`` the record's own ``device`` field
    is used, and an unlabelled record yields ``""``.  The identity is used
    exactly as the server stores it - no trimming, no case folding - because
    the server keys records by ``(device, sourceType, id)``.

    The record carries a single timestamp (``timeMs``, epoch milliseconds),
    so ``timestamp_start`` and ``timestamp_end`` are equal.  ``None`` is
    returned for a record whose text is missing or blank.  Malformed numeric
    fields raise ``ValueError`` and oversized text is rejected rather than
    silently truncated, because the raw values are the caller's contract.
    """
    if not isinstance(record, Mapping):
        raise TypeError("cloud record must be a mapping")
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > MAX_TEXT:
        raise ValueError(
            f"cloud record text exceeds the {MAX_TEXT} character contract limit"
        )

    record_id = _as_int(record.get("id"), "id")
    time_ms = _as_int(record.get("timeMs"), "timeMs")
    if device is None:
        raw_device = record.get("device")
        device = raw_device if isinstance(raw_device, str) else ""

    moment = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
    return TranscriptSegment(
        segment_id=f"cloud-transcript-sync:{device}:{record_id}",
        timestamp_start=moment,
        timestamp_end=moment,
        text=text,
        source=SOURCE,
    )
