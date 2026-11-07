"""Durable host-pull leases; a restart never re-executes host work."""
from __future__ import annotations

import math
import time
from pathlib import Path

from said_done.storage.repositories import Repository
from said_done.contracts import ExecutionResult, TaskPacket
from said_done.broker.state_machine import check_transition
from said_done.executor.artifact_validator import validate_artifacts
from said_done.executor.config import ExecutionConfig
from said_done.executor.workspace import collect_artifacts


def _event(conn, task_id: str, detail: str) -> None:
    conn.execute('INSERT INTO events(task_id,state,detail,created) VALUES(?,?,?,?)',
                 (task_id,'RUNNING',detail,time.time()))


def expire_leases(conn, now: float) -> None:
    """Clear expired leases once, retaining RUNNING and all existing effects."""
    rows=conn.execute('SELECT task_id FROM host_tasks WHERE lease_until<=?',(now,)).fetchall()
    for row in rows:
        _event(conn,row['task_id'],'host lease expired; effects unknown; reconcile before resuming')
    conn.execute('UPDATE host_tasks SET claimed_at=NULL,lease_until=NULL WHERE lease_until<=?',(now,))


def claim_next_task(repository: Repository, lease_seconds: float=7200) -> dict | None:
    lease_seconds=float(lease_seconds)
    if not math.isfinite(lease_seconds) or lease_seconds<=0:
        raise ValueError('lease_seconds must be positive and finite')
    with repository.db.transaction() as conn:
        now=time.time()
        expire_leases(conn,now)
        row=conn.execute("SELECT t.* FROM tasks t JOIN host_tasks h ON h.task_id=t.id "
                         "WHERE t.state='RUNNING' AND h.claimed_at IS NULL ORDER BY t.created LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute('UPDATE host_tasks SET claimed_at=?,lease_until=? WHERE task_id=?',
                     (now,now+lease_seconds,row['id']))
        _event(conn,row['id'],'host task claimed; reconcile existing artifacts before resuming')
        return {'task_id':row['id'],'task':TaskPacket.model_validate_json(row['body']).model_dump(mode='json'),
                'workspace':row['workspace'],'claimed_at':now,'lease_until':now+lease_seconds,
                'instructions':'Inspect existing artifacts first: previous host effects may exist. Write only inside output/.'}


def _claimed(conn, task_id: str):
    row=conn.execute('SELECT t.*,h.claimed_at,h.lease_until FROM tasks t JOIN host_tasks h ON h.task_id=t.id '
                     'WHERE t.id=?',(task_id,)).fetchone()
    if row is None or row['state']!='RUNNING':
        raise ValueError('task is not a running host task')
    if row['claimed_at'] is None or row['lease_until']<=time.time():
        raise ValueError('host task has no active lease; claim it first')
    return row


def complete_task(repository: Repository, task_id: str, summary: str, validate: bool=True) -> dict:
    # Validation is mandatory even if an older caller explicitly sends False.
    # A host narrative must never be enough to mark unverified outputs complete.
    with repository.db.transaction() as conn:
        row=_claimed(conn,task_id)
        workspace=Path(row['workspace'])
        task=TaskPacket.model_validate_json(row['body'])
        result=ExecutionResult(task_id=task_id,status='COMPLETED',summary=str(summary)[:4000],
                               artifacts=[str(p) for p in collect_artifacts(workspace,ExecutionConfig())])
        try:
            errors=validate_artifacts(task,result,workspace)
        except Exception:
            errors=['artifact validation failed']
        if errors:
            # Do not echo artifact content, model text, or user-controlled paths.
            result=ExecutionResult(task_id=task_id,status='FAILED',summary='artifact verification failed',
                                   error='host artifacts did not pass validation')
        check_transition(row['state'],result.status)
        conn.execute('UPDATE tasks SET state=?,result=?,error=? WHERE id=?',
                     (result.status,result.model_dump_json(),result.error,task_id))
        conn.execute('UPDATE host_tasks SET claimed_at=NULL,lease_until=NULL WHERE task_id=?',(task_id,))
        conn.execute('INSERT INTO events(task_id,state,detail,created) VALUES(?,?,?,?)',
                     (task_id,result.status,result.error or 'host artifacts verified',time.time()))
        return result.model_dump(mode='json')


def release_task(repository: Repository, task_id: str) -> dict:
    with repository.db.transaction() as conn:
        _claimed(conn,task_id)
        conn.execute('UPDATE host_tasks SET claimed_at=NULL,lease_until=NULL WHERE task_id=?',(task_id,))
        _event(conn,task_id,'host lease released; effects may exist; reconcile before resuming')
        return {'task_id':task_id,'released':True,'state':'RUNNING'}
