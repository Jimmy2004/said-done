# -*- coding: utf-8 -*-
"""Screening audit: durable admission decisions, failures, and the scheduler gates."""
import time

import pytest

from said_done.broker.task_manager import TaskManager
from said_done.config import Settings
from said_done.contracts import *
from said_done.semantic.minimax_adapter import MiniMaxError
from said_done.storage.audit import ACCEPTED, ERROR, REJECTED, AuditEntry
from said_done.storage.database import Database
from said_done.storage.repositories import Repository


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


def make(tmp_path, gate, **kwargs):
    from said_done.heartbeat.scheduler import Scheduler
    settings = Settings(data_dir=tmp_path, retry_delay=0, max_classification_attempts=2)
    repo = Repository(Database(tmp_path / 'core.db'))
    executor = TestExecutor()
    broker = TaskManager(repo.db, executor, tmp_path / 'work', lambda *a: [])
    scheduler = Scheduler(settings, repo, gate, broker, **kwargs)
    return scheduler, repo, executor


def ingest(repo, text='我需要一个 XRD 原理报告大纲', sid='s1', source='mock'):
    now = utcnow()
    repo.ingest([TranscriptSegment(segment_id=sid, timestamp_start=now, timestamp_end=now,
                                   text=text, source=source)])


class ApproveGate:
    def __init__(self, model='MiniMax-M3.1-Flash'):
        self.calls = 0
        self.model = model
    def classify(self, batch):
        self.calls += 1
        segment = batch.segments[-1]
        task = TaskPacket(task_type='CREATE_DOCUMENT', goal='XRD 原理报告大纲',
                          evidence_segment_ids=[segment.segment_id],
                          original_evidence=[segment.text],
                          expected_outputs=['outline.docx'], dedup_key='xrd-outline')
        return SemanticResponse(decisions=[TaskDecision(action='APPROVE', reason='明确的任务意图', task=task)])


class IgnoreGate:
    def __init__(self):
        self.calls = 0
    def classify(self, batch):
        self.calls += 1
        return SemanticResponse(decisions=[TaskDecision(action='IGNORE', reason='闲聊，没有明确任务')])


class EmptyGate:
    def __init__(self):
        self.calls = 0
    def classify(self, batch):
        self.calls += 1
        return SemanticResponse(decisions=[])


class FailingGate:
    def __init__(self, exc=None):
        self.calls = 0
        self.exc = exc or RuntimeError('provider offline')
    def classify(self, batch):
        self.calls += 1
        raise self.exc


def test_accepted_decision_is_audited_with_reason_model_and_evidence(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    ingest(repo)
    result = scheduler.tick(force=True)
    assert result['accepted'] == 1
    records = repo.audit_records()
    assert len(records) == 1
    row = records[0]
    assert row['outcome'] == ACCEPTED
    assert row['action'] == 'APPROVE'
    assert row['reason'] == '明确的任务意图'
    assert row['model'] == 'MiniMax-M3.1-Flash'
    assert row['task_id']
    assert row['linked_task_ids'] == [row['task_id']]
    assert row['evidence_segment_ids'] == ['s1']
    assert row['source_refs'][0]['source'] == 'mock'
    assert row['source_refs'][0]['segment_id'] == 's1'
    assert row['source_refs'][0]['timestamp_start']
    assert row['created'] > 0 and row['created_iso']
    assert row['batch_id']
    assert row['attempt'] == 1
    tasks = repo.status()['tasks']
    assert len(tasks) == 1 and tasks[0]['id'] == row['task_id']


def test_rejected_decision_is_audited_without_a_task(tmp_path):
    scheduler, repo, executor = make(tmp_path, IgnoreGate())
    ingest(repo)
    result = scheduler.tick(force=True)
    assert result['rejected'] == 1
    rows = repo.audit_records()
    assert len(rows) == 1
    row = rows[0]
    assert row['outcome'] == REJECTED
    assert row['action'] == 'IGNORE'
    assert row['reason'] == '闲聊，没有明确任务'
    assert row['task_id'] is None
    assert row['linked_task_ids'] == []
    assert row['error'] is None
    assert repo.status()['tasks'] == []


def test_empty_decisions_are_audited_as_no_candidate(tmp_path):
    scheduler, repo, executor = make(tmp_path, EmptyGate())
    ingest(repo)
    scheduler.tick(force=True)
    rows = repo.audit_records()
    assert len(rows) == 1
    assert rows[0]['outcome'] == REJECTED
    assert rows[0]['action'] == 'NO_CANDIDATE'
    assert rows[0]['reason']
    assert rows[0]['task_id'] is None
    assert rows[0]['source_refs'][0]['segment_id'] == 's1'
    assert repo.status()['tasks'] == []


def test_rejection_cites_only_the_newly_screened_segments(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    ingest(repo)
    scheduler.tick(force=True)
    scheduler.semantic = IgnoreGate()
    ingest(repo, '今天天气不错', 's2')
    scheduler.tick(force=True)
    rows = repo.audit_records()
    rejected = [row for row in rows if row['outcome'] == REJECTED][0]
    assert [ref['segment_id'] for ref in rejected['source_refs']] == ['s2']
    accepted = [row for row in rows if row['outcome'] == ACCEPTED][0]
    assert [ref['segment_id'] for ref in accepted['source_refs']] == ['s1']


def test_classification_failure_is_not_recorded_as_a_rejection(tmp_path):
    scheduler, repo, executor = make(tmp_path, FailingGate())
    ingest(repo)
    scheduler.tick(force=True)
    rows = repo.audit_records()
    assert len(rows) == 1
    row = rows[0]
    assert row['outcome'] == ERROR
    assert row['error']
    assert row['reason'] is None
    assert row['action'] is None
    assert row['task_id'] is None
    assert repo.audit_records(outcome=REJECTED) == []
    assert repo.status()['batches'][0]['status'] == 'RETRY'
    assert repo.status()['pending'][0]['count'] == 1


def test_failure_text_never_echoes_the_transcript(tmp_path):
    class EchoingError(RuntimeError):
        pass
    scheduler, repo, executor = make(tmp_path, FailingGate(EchoingError('我需要一个 XRD 原理报告大纲')))
    ingest(repo)
    scheduler.tick(force=True)
    error = repo.audit_records()[0]['error']
    assert error == 'EchoingError'
    assert 'XRD' not in error


def test_provider_failure_keeps_the_sanitized_message(tmp_path):
    scheduler, repo, executor = make(
        tmp_path, FailingGate(MiniMaxError('MiniMax classification failed after 3 attempts (TypeError)')))
    ingest(repo)
    scheduler.tick(force=True)
    error = repo.audit_records()[0]['error']
    assert 'MiniMaxError' in error
    assert 'MiniMax classification failed' in error


def test_failed_attempts_are_numbered_and_durable(tmp_path):
    scheduler, repo, executor = make(tmp_path, FailingGate())
    ingest(repo)
    scheduler.tick(force=True)
    scheduler.tick(force=True)
    rows = repo.audit_records()
    assert len(rows) == 2
    assert sorted(row['attempt'] for row in rows) == [1, 2]
    assert repo.status()['batches'][0]['status'] == 'EXHAUSTED'
    reopened = Repository(Database(tmp_path / 'core.db'))
    assert len(reopened.audit_records()) == 2


def test_audit_rows_survive_restart(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    ingest(repo)
    scheduler.tick(force=True)
    reopened = Repository(Database(tmp_path / 'core.db'))
    rows = reopened.audit_records()
    assert len(rows) == 1
    assert rows[0]['outcome'] == ACCEPTED
    assert rows[0]['task_id'] == repo.audit_records()[0]['task_id']


def test_audit_write_failure_rolls_back_the_classification(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    ingest(repo)
    def broken(conn, entries):
        raise RuntimeError('audit storage unavailable')
    scheduler.repository.record_screening_audit = broken
    scheduler.tick(force=True)
    assert repo.status()['tasks'] == []
    assert repo.status()['batches'][0]['status'] == 'RETRY'
    assert repo.status()['pending'][0]['count'] == 1
    assert repo.audit_records() == []


def test_duplicate_approval_stays_accepted_and_links_the_existing_task(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate(), screening_only=True)
    ingest(repo)
    scheduler.tick(force=True)
    first = repo.audit_records()[0]
    assert first['outcome'] == ACCEPTED
    assert first['detail'] is None
    ingest(repo, '我需要一个 XRD 原理报告大纲', 's2')
    scheduler.tick(force=True)
    rows = repo.audit_records()
    assert len(rows) == 2
    second = [row for row in rows if row['detail']][0]
    assert second['outcome'] == ACCEPTED
    assert second['action'] == 'APPROVE'
    assert 'duplicate' in second['detail']
    assert second['task_id'] == first['task_id']
    assert second['linked_task_ids'] == [first['task_id']]
    assert second['reason'] == '明确的任务意图'
    assert len(repo.status()['tasks']) == 1


def test_audit_entry_keeps_failures_out_of_the_rejection_space():
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome=ERROR, model='m')
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome=ERROR, model='m', error='boom', reason='model said no')
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome=ERROR, model='m', error='boom', action='IGNORE')
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome=ACCEPTED, model='m')
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome=ACCEPTED, model='m', task_id='t', error='boom')
    with pytest.raises(ValueError):
        AuditEntry(batch_id='b', outcome='MAYBE', model='m')
    assert AuditEntry(batch_id='b', outcome=ACCEPTED, model='m', task_id='t').task_id == 't'
    assert AuditEntry(batch_id='b', outcome=REJECTED, model='m', action='NO_CANDIDATE').action == 'NO_CANDIDATE'


def test_audit_query_filters_and_summary(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    ingest(repo)
    scheduler.tick(force=True)
    scheduler.semantic = IgnoreGate()
    ingest(repo, '今天天气不错', 's2')
    scheduler.tick(force=True)
    scheduler.semantic = FailingGate()
    ingest(repo, '另一句闲聊', 's3')
    scheduler.tick(force=True)
    assert repo.audit_summary()['total'] == 3
    assert repo.audit_summary()['by_outcome'] == {ACCEPTED: 1, REJECTED: 1, ERROR: 1}
    assert repo.audit_summary()['by_action']['APPROVE'] == 1
    assert len(repo.audit_records(outcome=ERROR)) == 1
    assert len(repo.audit_records(action='APPROVE')) == 1
    assert len(repo.audit_records(limit=2)) == 2
    assert repo.audit_records()[0]['created'] >= repo.audit_records()[1]['created']
    task_id = repo.audit_records(outcome=ACCEPTED)[0]['task_id']
    assert [row['task_id'] for row in repo.audit_records(task_id=task_id)] == [task_id]
    assert len(repo.audit_records(since=time.time() + 60)) == 0


def test_screening_only_queues_approved_tasks_without_execution(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate(), screening_only=True)
    ingest(repo)
    result = scheduler.tick(force=True)
    assert result['screening_only'] is True
    assert result['accepted'] == 1
    tasks = repo.status()['tasks']
    assert len(tasks) == 1 and tasks[0]['state'] == 'QUEUED'
    assert executor.count == 0
    assert repo.audit_records()[0]['outcome'] == ACCEPTED


def test_screening_only_skips_recovery(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate(), screening_only=True)
    with repo.db.transaction() as conn:
        conn.execute('INSERT INTO tasks(id,dedup_key,body,state,created) VALUES(?,?,?,?,?)',
                     ('running-task', 'running-key', '{}', 'RUNNING', time.time()))
    scheduler.tick(force=True)
    assert repo.status()['tasks'][0]['state'] == 'RUNNING'


def test_recovery_still_runs_outside_screening_mode(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    with repo.db.transaction() as conn:
        conn.execute('INSERT INTO tasks(id,dedup_key,body,state,created) VALUES(?,?,?,?,?)',
                     ('running-task', 'running-key', '{}', 'RUNNING', time.time()))
    scheduler.tick(force=True)
    assert repo.status()['tasks'][0]['state'] == 'FAILED'


def test_poll_source_false_stops_the_tick_before_downstream_work(tmp_path):
    calls = []
    def poll():
        calls.append(1)
        return False
    gate = ApproveGate()
    scheduler, repo, executor = make(tmp_path, gate, poll_source=poll)
    ingest(repo)
    result = scheduler.tick(force=True)
    assert result['polled'] is False
    assert result['screened'] == 0
    assert calls == [1]
    assert gate.calls == 0
    assert repo.audit_records() == []
    assert repo.status()['pending'][0]['count'] == 1
    assert repo.status()['batches'] == []


def test_poll_source_true_allows_classification(tmp_path):
    def poll():
        return True
    scheduler, repo, executor = make(tmp_path, ApproveGate(), poll_source=poll)
    ingest(repo)
    result = scheduler.tick(force=True)
    assert result['polled'] is True
    assert result['accepted'] == 1
    assert repo.audit_records()[0]['outcome'] == ACCEPTED


def test_poll_source_runs_before_recovery(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate())
    with repo.db.transaction() as conn:
        conn.execute('INSERT INTO tasks(id,dedup_key,body,state,created) VALUES(?,?,?,?,?)',
                     ('running-task', 'running-key', '{}', 'RUNNING', time.time()))
    seen = []
    def poll():
        seen.append(repo.status()['tasks'][0]['state'])
        return True
    scheduler.poll_source = poll
    scheduler.tick(force=True)
    assert seen == ['RUNNING']
    assert repo.status()['tasks'][0]['state'] == 'FAILED'


def test_cancellation_is_audited_with_the_affected_task(tmp_path):
    scheduler, repo, executor = make(tmp_path, ApproveGate(), screening_only=True)
    ingest(repo)
    scheduler.tick(force=True)
    task_id = repo.audit_records()[0]['task_id']
    class CancelGate:
        def classify(self, batch):
            return SemanticResponse(decisions=[TaskDecision(action='CANCEL', reason='用户取消',
                                                              target_dedup_key='xrd-outline')])
    scheduler.semantic = CancelGate()
    ingest(repo, '取消 XRD 原理报告大纲', 's2')
    scheduler.tick(force=True)
    rows = repo.audit_records()
    cancel = [row for row in rows if row['action'] == 'CANCEL'][0]
    assert cancel['outcome'] == REJECTED
    assert cancel['reason'] == '用户取消'
    assert cancel['target_dedup_key'] == 'xrd-outline'
    assert cancel['linked_task_ids'] == [task_id]
    assert repo.status()['tasks'][0]['state'] == 'CANCELLED'
