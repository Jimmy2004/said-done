"""Tests for the incremental cloud transcript cursor and bounded reconcile."""
import pytest

from said_done.ingestion.cloud_poll import CloudTranscriptPoll, cloud_status
from said_done.storage.database import Database
from said_done.storage.repositories import Repository


def transcript(rid, ms, text):
    return dict(id=rid, timeMs=ms, sourceType='transcript', text=text)


class Cloud:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.failure = False

    def fetch(self, args):
        self.calls.append(args)
        if self.failure:
            raise OSError('provider payload should not appear in logs')
        rows = [r for r in sorted(self.rows, key=lambda r: r['id'])
                if args['fromMs'] <= r['timeMs'] < args['toMs'] and r['id'] > args['sinceId']]
        rows = rows[:args['limit']]
        return dict(results=rows, resultCount=len(rows), truncated=False)


def test_cutover_and_late_sync_do_not_replay_or_drop(tmp_path):
    base = 1_794_000_000_000
    now = [base]
    repo = Repository(Database(tmp_path / 'core.db'))
    cloud = Cloud([transcript(1, base - 1, 'old')])
    poll = CloudTranscriptPoll(repo, cloud, clock_ms=lambda: now[0])
    assert poll()
    assert repo.pending() == []
    now[0] += 1000
    cloud.rows.append(transcript(3, base + 900, 'arrived first'))
    assert poll()
    now[0] += 1000
    cloud.rows.append(transcript(2, base + 300, 'lower ID arrived late'))
    assert CloudTranscriptPoll(repo, cloud, clock_ms=lambda: now[0])()
    assert cloud.calls[-1]['sinceId'] == 3
    assert [r.text for r in repo.pending()] == ['arrived first']
    # The hourly, time-bounded repair sees a lower ID without rewinding the
    # normal watermark, even when the worker is restarted.
    now[0] = base + poll.RECONCILE_INTERVAL_MS
    assert CloudTranscriptPoll(repo, cloud, clock_ms=lambda: now[0])()
    assert {r.text for r in repo.pending()} == {'arrived first', 'lower ID arrived late'}
    assert len(repo.pending()) == 2
    state = cloud_status(repo)[0]
    assert state['cutover_ms'] == base
    assert state['highwater_id'] == state['since_id'] == 3
    assert state['last_reconcile_ms'] == now[0]


def test_interrupted_page_resumes_without_advancing_past_unread_data(tmp_path):
    base = 1_794_000_000_000
    now = [base]
    repo = Repository(Database(tmp_path / 'core.db'))
    cloud = Cloud([])
    poll = CloudTranscriptPoll(repo, cloud, clock_ms=lambda: now[0])
    poll.PAGE_SIZE = poll.PAGES_PER_TICK = 1
    assert poll()
    now[0] += 10
    cloud.rows.extend([transcript(1, base + 1, 'one'), transcript(2, base + 2, 'two')])
    assert not poll()
    assert cloud_status(repo)[0]['since_id'] == 1
    cloud.failure = True
    with pytest.raises(RuntimeError, match='cursor retained') as exc:
        poll()
    assert 'provider payload' not in str(exc.value)
    assert cloud_status(repo)[0]['since_id'] == 1
    cloud.failure = False
    restarted = CloudTranscriptPoll(repo, cloud, clock_ms=lambda: now[0])
    assert restarted()
    assert cloud.calls[-1]['sinceId'] == 1
    assert len(repo.pending()) == 2
    state = cloud_status(repo)[0]
    assert state['last_error'] is None
    assert state['highwater_id'] == 2
