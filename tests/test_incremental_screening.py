# -*- coding: utf-8 -*-
"""Incremental screening: the model only ever receives new unclassified text."""
import json
import time
from datetime import timedelta

import httpx

from said_done.broker.task_manager import TaskManager
from said_done.config import Settings
from said_done.contracts import *
from said_done.heartbeat.scheduler import Scheduler
from said_done.semantic.minimax_adapter import MiniMaxAdapter
from said_done.storage.audit import ERROR
from said_done.storage.database import Database
from said_done.storage.repositories import Repository, segment_key


class TestExecutor:
    __test__ = False

    def __init__(self):
        self.count = 0

    def create_task(self, task, workspace):
        return task.task_id

    def preflight(self, task, workspace):
        return PreflightResult(status='READY')

    def execute(self, task, workspace):
        self.count += 1
        return ExecutionResult(task_id=task.task_id, status='COMPLETED', summary='completed')


class RecordingGate:
    """Stands in for MiniMax and records exactly what it was asked to screen."""

    def __init__(self, decisions=None):
        self.batches = []
        self.decisions = decisions or [TaskDecision(action='IGNORE', reason='recorded')]

    def classify(self, batch):
        self.batches.append(batch)
        return SemanticResponse(decisions=list(self.decisions))


class FailingGate:
    def __init__(self):
        self.batches = []

    def classify(self, batch):
        self.batches.append(batch)
        raise RuntimeError('provider offline')


def make(tmp_path, gate, **kwargs):
    settings = Settings(data_dir=tmp_path, retry_delay=0, max_classification_attempts=3)
    repo = Repository(Database(tmp_path / 'core.db'))
    broker = TaskManager(repo.db, TestExecutor(), tmp_path / 'work', lambda *a: [])
    scheduler = Scheduler(settings, repo, gate, broker, **kwargs)
    return scheduler, repo


def segment(text, sid, source='mock', offset=0):
    now = utcnow() + timedelta(seconds=offset)
    return TranscriptSegment(segment_id=sid, timestamp_start=now, timestamp_end=now + timedelta(seconds=1),
                             text=text, source=source)


def persist_task(repo, state='QUEUED'):
    task = TaskPacket(task_type='CREATE_DOCUMENT', goal='XRD 原理报告大纲',
                      evidence_segment_ids=['s1'],
                      original_evidence=['我需要一个 XRD 原理报告大纲'],
                      expected_outputs=['outline.docx'], dedup_key='xrd-outline')
    with repo.db.transaction() as conn:
        conn.execute('INSERT INTO tasks(id,dedup_key,fingerprint,body,state,created) VALUES(?,?,?,?,?,?)',
                     (task.task_id, task.dedup_key, 'fingerprint-1', task.model_dump_json(), state, time.time()))
    return task


def classify_legacy_batch(repo, context_text, context_sid, new_segment):
    """Store a batch the way it looked before incremental screening.

    The stored body carries an already classified transcript as context; only
    the new segment belongs to the batch.
    """
    repo.ingest([segment(context_text, context_sid)])
    first = repo.reserve_batch(repo.pending())
    with repo.db.transaction() as conn:
        conn.execute("UPDATE batches SET status='CLASSIFIED' WHERE id=?", (first.batch_id,))
        conn.execute('UPDATE segments SET classified=1 WHERE batch_id=?', (first.batch_id,))
    repo.ingest([new_segment])
    pending = repo.pending()
    body = TranscriptBatch(batch_id='legacy-batch',
                            segments=[segment(context_text, context_sid)] + pending)
    with repo.db.transaction() as conn:
        conn.execute("INSERT INTO batches(id,body,status,next_retry) VALUES(?,?,'RETRY',0)",
                     ('legacy-batch', body.model_dump_json()))
        for seg in pending:
            conn.execute('UPDATE segments SET batch_id=? WHERE id=? AND classified=0',
                         ('legacy-batch', segment_key(seg)))
    return first


def stored_body(repo, batch_id):
    with repo.db.connect() as conn:
        row = conn.execute('SELECT body FROM batches WHERE id=?', (batch_id,)).fetchone()
    return row['body']


def test_reserved_batch_carries_only_new_unclassified_text(tmp_path):
    repo = Repository(Database(tmp_path / 'core.db'))
    repo.ingest([segment('我需要一个 XRD 原理报告大纲', 'old')])
    first = repo.reserve_batch(repo.pending())
    with repo.db.transaction() as conn:
        conn.execute("UPDATE batches SET status='CLASSIFIED' WHERE id=?", (first.batch_id,))
        conn.execute('UPDATE segments SET classified=1 WHERE batch_id=?', (first.batch_id,))
    repo.ingest([segment('主题是 XRD 原理', 'new')])
    second = repo.reserve_batch(repo.pending())
    assert [s.segment_id for s in second.segments] == ['new']
    assert '我需要一个 XRD 原理报告大纲' not in second.model_dump_json()


def test_repeated_ticks_never_resend_classified_text(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    repo.ingest([segment('第一段历史内容', 's1')])
    scheduler.tick(force=True)
    repo.ingest([segment('第二段新内容', 's2')])
    scheduler.tick(force=True)
    assert [[s.text for s in b.segments] for b in gate.batches] == [['第一段历史内容'], ['第二段新内容']]
    screened = [s.segment_id for b in gate.batches for s in b.segments]
    assert len(screened) == len(set(screened)) == 2


def test_classified_segment_is_never_reserved_twice(tmp_path):
    scheduler, repo = make(tmp_path, RecordingGate())
    repo.ingest([segment('第一段历史内容', 's1')])
    scheduler.tick(force=True)
    scheduler.tick(force=True)
    scheduler.tick(force=True)
    assert repo.status()['pending'][0]['count'] == 0
    assert len(repo.retry_batches()) == 0


def test_known_task_identity_is_exposed_without_original_quotes(tmp_path):
    repo = Repository(Database(tmp_path / 'core.db'))
    task = persist_task(repo)
    context = repo.semantic_context(None)
    assert context == [{'task_id': task.task_id, 'dedup_key': 'xrd-outline',
                        'goal': 'XRD 原理报告大纲', 'state': 'QUEUED'}]
    assert 'original_evidence' not in context[0]
    assert '我需要一个 XRD 原理报告大纲' not in json.dumps(context, ensure_ascii=False)


def test_minimax_payload_carries_only_incremental_text(tmp_path):
    repo = Repository(Database(tmp_path / 'core.db'))
    persist_task(repo)
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        answer = SemanticResponse(decisions=[TaskDecision(action='IGNORE', reason='noise')])
        return httpx.Response(200, json={'base_resp': {'status_code': 0},
                                         'choices': [{'message': {'content': answer.model_dump_json()}}]})

    batch = TranscriptBatch(batch_id='b1', segments=[segment('第二段新内容', 's2')])
    gate = MiniMaxAdapter(api_key='test-only',
                           client=httpx.Client(transport=httpx.MockTransport(transport)),
                           context_provider=repo.semantic_context)
    gate.classify(batch)
    content = calls[0]['messages'][1]['content']
    payload = json.loads(content)
    assert [s['text'] for s in payload['transcript_batch']['segments']] == ['第二段新内容']
    assert '我需要一个 XRD 原理报告大纲' not in content
    assert payload['known_tasks'][0]['dedup_key'] == 'xrd-outline'
    assert 'original_evidence' not in payload['known_tasks'][0]


def test_cancellation_still_resolves_a_persisted_task_identity(tmp_path):
    repo = Repository(Database(tmp_path / 'core.db'))
    task = persist_task(repo)

    def transport(request):
        answer = SemanticResponse(decisions=[TaskDecision(action='CANCEL', reason='用户取消',
                                                          target_dedup_key='xrd-outline')])
        return httpx.Response(200, json={'base_resp': {'status_code': 0},
                                         'choices': [{'message': {'content': answer.model_dump_json()}}]})

    batch = TranscriptBatch(batch_id='b1', segments=[segment('取消 XRD 原理报告大纲', 's2')])
    gate = MiniMaxAdapter(api_key='test-only',
                           client=httpx.Client(transport=httpx.MockTransport(transport)),
                           context_provider=repo.semantic_context)
    response = gate.classify(batch)
    assert response.decisions[0].target_dedup_key == task.dedup_key


def test_legacy_batch_with_context_is_narrowed_and_still_screened(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    classify_legacy_batch(repo, '我需要一个 XRD 原理报告大纲', 'old',
                           segment('取消 XRD 原理报告大纲', 'new'))
    scheduler.tick(force=True)
    assert [[s.text for s in b.segments] for b in gate.batches] == [['取消 XRD 原理报告大纲']]
    assert '我需要一个 XRD 原理报告大纲' not in stored_body(repo, 'legacy-batch')
    statuses = {row['id']: row['status'] for row in repo.status()['batches']}
    assert statuses['legacy-batch'] == 'CLASSIFIED'
    # The narrowed-away segment is retained, not deleted.
    with repo.db.connect() as conn:
        rows = {r['id']: r['classified'] for r in conn.execute('SELECT id,classified FROM segments')}
    assert rows[segment_key(segment('我需要一个 XRD 原理报告大纲', 'old'))] == 1
    assert rows[segment_key(segment('取消 XRD 原理报告大纲', 'new'))] == 1
    assert repo.status()['pending'][0]['count'] == 0


def test_narrowed_legacy_retry_keeps_retrying_without_replaying_history(tmp_path):
    gate = FailingGate()
    scheduler, repo = make(tmp_path, gate)
    classify_legacy_batch(repo, '我需要一个 XRD 原理报告大纲', 'old',
                           segment('取消 XRD 原理报告大纲', 'new'))
    scheduler.tick(force=True)
    scheduler.tick(force=True)
    assert [[s.text for s in b.segments] for b in gate.batches] == [
        ['取消 XRD 原理报告大纲'], ['取消 XRD 原理报告大纲']]
    statuses = {row['id']: row['status'] for row in repo.status()['batches']}
    assert statuses['legacy-batch'] == 'RETRY'
    assert repo.status()['pending'][0]['count'] == 1


def test_context_of_one_cycle_is_not_replayed_in_the_next(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    repo.ingest([segment('我需要一个报告大纲', 's1')])
    scheduler.tick(force=True)
    repo.ingest([segment('主题是 XRD 原理', 's2')])
    scheduler.tick(force=True)
    assert [[s.text for s in b.segments] for b in gate.batches] == [
        ['我需要一个报告大纲'], ['主题是 XRD 原理']]
    assert repo.status()['tasks'] == []

def test_orphaned_batch_is_never_handed_to_the_model(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    body = TranscriptBatch(batch_id='orphan', segments=[segment('我需要一个 XRD 原理报告大纲', 's1')])
    with repo.db.transaction() as conn:
        conn.execute("INSERT INTO batches(id,body,status,next_retry) VALUES(?,?,'PENDING',0)",
                     ('orphan', body.model_dump_json()))
    scheduler.tick(force=True)
    assert gate.batches == []
    assert repo.status()['batches'][0]['status'] == 'RETRY'
    assert repo.status()['batches'][0]['error']
    assert len(repo.audit_records(outcome=ERROR)) == 1


def test_batch_body_must_match_the_segments_it_owns(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    repo.ingest([segment('真正要筛选的内容', 's1')])
    body = TranscriptBatch(batch_id='mismatch', segments=[segment('一段无关的旧内容', 'old')])
    with repo.db.transaction() as conn:
        conn.execute("INSERT INTO batches(id,body,status,next_retry) VALUES(?,?,'PENDING',0)",
                     ('mismatch', body.model_dump_json()))
        conn.execute('UPDATE segments SET batch_id=? WHERE id=?',
                     ('mismatch', segment_key(repo.pending()[0])))
    scheduler.tick(force=True)
    assert gate.batches == []
    assert repo.status()['batches'][0]['status'] == 'RETRY'
    assert repo.status()['batches'][0]['error']


def test_already_classified_batch_is_not_screened_again(tmp_path):
    gate = RecordingGate()
    scheduler, repo = make(tmp_path, gate)
    repo.ingest([segment('第一段历史内容', 's1')])
    scheduler.tick(force=True)
    assert len(gate.batches) == 1
    # Put the finished batch back into the queue: its text is already
    # classified, so it must not be sent to the model a second time.
    with repo.db.transaction() as conn:
        conn.execute("UPDATE batches SET status='RETRY',next_retry=0")
    scheduler.tick(force=True)
    assert len(gate.batches) == 1
    assert repo.status()['batches'][0]['status'] == 'RETRY'
    assert repo.status()['batches'][0]['error']
