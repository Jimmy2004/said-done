"""Configurable batch admission triggers; an empty keyword list disables triggers."""
from collections.abc import Sequence
from datetime import datetime

from said_done.config import DEFAULT_TRIGGER_KEYWORDS
from said_done.contracts import TranscriptSegment, utcnow


def should_process(segments: Sequence[TranscriptSegment], min_content_chars: int = 200,
                   max_batch_wait: int = 1800, now: datetime | None = None,
                   keywords: Sequence[str] | None = None) -> bool:
    keywords = DEFAULT_TRIGGER_KEYWORDS if keywords is None else keywords
    if not segments:
        return False
    now = now or utcnow()
    return (sum(len(s.text.strip()) for s in segments) >= min_content_chars
            or any(any(k in s.text for k in keywords) for s in segments)
            or (now - min(s.timestamp_start for s in segments)).total_seconds() >= max_batch_wait)
