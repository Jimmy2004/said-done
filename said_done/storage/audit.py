"""Durable screening audit trail for the MiniMax admission gate.

Every classification outcome is recorded before the batch state advances.
Accepted work, rejected work and classification failures are three distinct
outcomes: a failure is never stored as a rejection, so the audit can be read
as "what did the gate decide, and why".

Rows are written inside the caller's transaction. An audit row therefore
exists if and only if the classification (or the retry bookkeeping) it
describes was committed.
"""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

ACCEPTED = 'ACCEPTED'
REJECTED = 'REJECTED'
ERROR = 'ERROR'
OUTCOMES = (ACCEPTED, REJECTED, ERROR)

COLUMNS = ('id', 'batch_id', 'outcome', 'action', 'model', 'reason', 'error',
           'detail', 'source_refs', 'evidence_segment_ids', 'task_id',
           'linked_task_ids', 'dedup_key', 'target_dedup_key', 'attempt', 'created')

INSERT_SQL = ('INSERT INTO screening_audit ('
              'id,batch_id,outcome,action,model,reason,error,detail,source_refs,'
              'evidence_segment_ids,task_id,linked_task_ids,dedup_key,target_dedup_key,attempt,created) '
              'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)')


@dataclass(frozen=True)
class AuditEntry:
    """One screening decision or one classification failure."""

    batch_id: str
    outcome: str
    model: str
    action: str | None = None
    reason: str | None = None
    error: str | None = None
    detail: str | None = None
    source_refs: tuple = ()
    evidence_segment_ids: tuple = ()
    task_id: str | None = None
    linked_task_ids: tuple = ()
    dedup_key: str | None = None
    target_dedup_key: str | None = None
    attempt: int = 1
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f'unknown screening outcome {self.outcome!r}')
        if self.outcome == ERROR:
            if not (self.error or '').strip():
                raise ValueError('a failure row needs an error summary')
            if self.reason or self.action or self.task_id or self.linked_task_ids:
                raise ValueError('a classification failure is not a screening decision')
        else:
            if self.error is not None:
                raise ValueError('only failure rows carry an error summary')
            if self.outcome == ACCEPTED and not self.task_id and not self.detail:
                raise ValueError('accepted screening must link a task or explain why it cannot')

    def row(self):
        return (self.id, self.batch_id, self.outcome, self.action, self.model,
                self.reason, self.error, self.detail,
                json.dumps(list(self.source_refs), ensure_ascii=False),
                json.dumps(list(self.evidence_segment_ids), ensure_ascii=False),
                self.task_id, json.dumps(list(self.linked_task_ids), ensure_ascii=False),
                self.dedup_key, self.target_dedup_key, int(self.attempt), float(self.created))


def source_refs(batch, segment_ids=None):
    """Evidence references for the cited segments, or for the whole batch."""
    segments = list(batch.segments)
    if segment_ids:
        wanted = set(segment_ids)
        cited = [s for s in segments if s.segment_id in wanted]
        if cited:
            segments = cited
    return ({'source': s.source, 'segment_id': s.segment_id,
             'timestamp_start': s.timestamp_start.isoformat(),
             'timestamp_end': s.timestamp_end.isoformat()} for s in segments)


def decode(row):
    """Turn a stored row into a plain dictionary for the CLI."""
    record = dict(row)
    for key in ('source_refs', 'evidence_segment_ids', 'linked_task_ids'):
        try:
            record[key] = json.loads(record[key] or '[]')
        except (TypeError, ValueError):
            record[key] = []
    created = record.get('created')
    record['created_iso'] = (datetime.fromtimestamp(created, timezone.utc).isoformat()
                              if isinstance(created, (int, float)) else None)
    return record


def query(conn, limit=100, outcome=None, action=None, batch_id=None, task_id=None,
          since=None, until=None):
    """Newest first; filters combine with AND."""
    clauses, params = [], []
    if outcome:
        clauses.append('outcome=?')
        params.append(outcome)
    if action:
        clauses.append('action=?')
        params.append(action)
    if batch_id:
        clauses.append('batch_id=?')
        params.append(batch_id)
    if task_id:
        clauses.append('(task_id=? OR linked_task_ids LIKE ?)')
        params.extend([task_id, f'%"{task_id}"%'])
    if since is not None:
        clauses.append('created>=?')
        params.append(float(since))
    if until is not None:
        clauses.append('created<?')
        params.append(float(until))
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    params.append(max(1, int(limit)))
    sql = ('SELECT ' + ','.join(COLUMNS) + ' FROM screening_audit' + where +
           ' ORDER BY created DESC, rowid DESC LIMIT ?')
    return [decode(row) for row in conn.execute(sql, params)]


def summary(conn, since=None, until=None):
    """Counts by outcome and by decision action."""
    clauses, params = [], []
    if since is not None:
        clauses.append('created>=?')
        params.append(float(since))
    if until is not None:
        clauses.append('created<?')
        params.append(float(until))
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    by_outcome = {row['outcome']: row['count'] for row in conn.execute(
        'SELECT outcome, COUNT(*) AS count FROM screening_audit' + where +
        ' GROUP BY outcome', params)}
    by_action = {row['action']: row['count'] for row in conn.execute(
        'SELECT action, COUNT(*) AS count FROM screening_audit' + where +
        (' AND ' if where else ' WHERE ') + 'action IS NOT NULL GROUP BY action', params)}
    total = sum(by_outcome.values())
    return {'total': total, 'by_outcome': by_outcome, 'by_action': by_action}
