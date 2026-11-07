"""Source plugin contract and durable per-source context."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from said_done.contracts import TranscriptSegment


@dataclass
class PollResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    complete: bool = True


class SourceContext:
    def __init__(self, repository, source_id: str, clock: Callable[[],float] = time.time):
        self.repository=self.repo=repository
        self.source_id=source_id
        self.clock=clock

    def get(self, key: str, default=None):
        with self.repo.db.connect() as conn:
            row=conn.execute('SELECT value FROM source_state WHERE source_id=? AND key=?',
                             (self.source_id,key)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key: str, value) -> None:
        with self.repo.db.transaction() as conn:
            conn.execute('INSERT INTO source_state(source_id,key,value) VALUES(?,?,?) '
                         'ON CONFLICT(source_id,key) DO UPDATE SET value=excluded.value',
                         (self.source_id,key,json.dumps(value)))

    def ingest(self, segments) -> int:
        return self.repo.ingest(segments)


class TranscriptSource(Protocol):
    id: str
    kind: str
    def poll(self, ctx: SourceContext) -> PollResult: ...
    def next_deadline(self, now: float) -> float | None: ...
    def status(self) -> dict: ...
    def close(self) -> None: ...
