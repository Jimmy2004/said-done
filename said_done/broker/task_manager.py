import hashlib
import json
import time
from pathlib import Path
from said_done.contracts import TaskPacket, ExecutionResult
from said_done.broker.state_machine import check_transition
from said_done.broker.deduplication import task_fingerprint,provenance_fingerprint

class TaskManager:
    def __init__(self, db, executor, work_root: Path, validate_artifacts):
        self.db,self.executor,self.work_root,self.validate_artifacts = db,executor,Path(work_root),validate_artifacts

    def transition(self, conn, task_id, new, detail=''):
        old = conn.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()['state']
        check_transition(old,new)
        conn.execute('UPDATE tasks SET state=?,error=? WHERE id=?',(new,detail or None,task_id))
        conn.execute('INSERT INTO events(task_id,state,detail,created) VALUES(?,?,?,?)',(task_id,new,detail,time.time()))

    def accept(self, conn, response, batch=None):
        # Resolve approvals first, then cancellations: a later withdrawal in a
        # merged transcript must not leave newly approved work in the queue.
        ordered = sorted(response.decisions,key=lambda d: d.action != 'APPROVE')
        # Distinct tasks extracted together may share a source paragraph.
        # Provenance suppresses replays from earlier classifications, while
        # within-response intent separation remains the semantic gate's job.
        prior_provenance={provenance_fingerprint(d.task,batch) for d in ordered if d.task
                          if conn.execute('SELECT 1 FROM task_provenance WHERE fingerprint=?',(provenance_fingerprint(d.task,batch),)).fetchone()}
        for decision in ordered:
            if decision.action in {'CANCEL','ALREADY_COMPLETED'} and decision.target_dedup_key:
                rows = conn.execute("SELECT id FROM tasks WHERE dedup_key=? AND state IN ('QUEUED','PREFLIGHT')",(decision.target_dedup_key,)).fetchall()
                for row in rows:
                    self.transition(conn,row['id'],'CANCELLED',decision.reason)
                continue
            if decision.action != 'APPROVE':
                continue
            task=decision.task
            provenance=provenance_fingerprint(task,batch)
            if provenance in prior_provenance:
                continue
            fingerprint=task_fingerprint(task.task_type,task.goal,task.input_paths,task.expected_outputs)
            inserted=conn.execute('INSERT OR IGNORE INTO tasks(id,dedup_key,fingerprint,body,state,created) VALUES(?,?,?,?,?,?)',
                (task.task_id,task.dedup_key,fingerprint,task.model_dump_json(),'DETECTED',time.time())).rowcount
            if inserted:
                conn.execute('INSERT INTO task_provenance(fingerprint,task_id) VALUES(?,?)',(provenance,task.task_id))
                self.transition(conn,task.task_id,'CLASSIFIED')
                self.transition(conn,task.task_id,'QUEUED')

    def recover(self):
        with self.db.transaction() as conn:
            from said_done.broker.host_queue import expire_leases
            expire_leases(conn,time.time())
            # An interrupted execution may already have effects. Never automatically run it again.
            for r in conn.execute("SELECT id,state FROM tasks WHERE state IN ('RUNNING','PREFLIGHT') AND NOT EXISTS (SELECT 1 FROM host_tasks h WHERE h.task_id=tasks.id)").fetchall():
                if r['state']=='RUNNING':
                    self.transition(conn,r['id'],'FAILED','execution interrupted; effects unknown; manual reconciliation required')
                else:
                    self.transition(conn,r['id'],'BLOCKED','preflight interrupted; explicit retry required')

    def run_queued(self, max_tasks=1):
        # Classify new input between task slots so queued work can be cancelled.
        for _ in range(max_tasks):
            with self.db.transaction() as conn:
                row=conn.execute("SELECT * FROM tasks WHERE state='QUEUED' ORDER BY created LIMIT 1").fetchone()
                if not row:
                    return
                task=TaskPacket.model_validate_json(row['body'])
                self.transition(conn,task.task_id,'PREFLIGHT')
            # Never use model-controlled task_id as a filesystem path.
            workspace=self.work_root/hashlib.sha256(task.task_id.encode()).hexdigest()[:24]
            workspace.mkdir(parents=True,exist_ok=True)
            try:
                self.executor.create_task(task,workspace)
                preflight=self.executor.preflight(task,workspace)
                if preflight.status != 'READY':
                    with self.db.transaction() as conn:
                        self.transition(conn,task.task_id,'BLOCKED','; '.join(preflight.reasons))
                    continue
                host_mode=getattr(self.executor,'host_mode',False) or getattr(self.executor,'kind','') in {'host','claude-desktop','chatgpt-app'}
                if host_mode:
                    from said_done.executor.preflight import resolve_inputs
                    from said_done.executor.workspace import prepare_workspace
                    inputs,errors=resolve_inputs(task,workspace,self.executor.preflight_config)
                    if errors:
                        raise ValueError('host input resolution failed')
                    prepare_workspace(task,workspace,inputs,self.executor.execution_config)
                with self.db.transaction() as conn:
                    state=conn.execute('SELECT state FROM tasks WHERE id=?',(task.task_id,)).fetchone()['state']
                    if state=='CANCELLED':
                        continue
                    self.transition(conn,task.task_id,'RUNNING')
                    conn.execute('UPDATE tasks SET workspace=? WHERE id=?',(str(workspace),task.task_id))
                    if host_mode:
                        conn.execute('INSERT INTO host_tasks(task_id) VALUES(?)',(task.task_id,))
                if host_mode:
                    continue
                result=self.executor.execute(task,workspace)
                if result.task_id != task.task_id:
                    raise ValueError('executor task identity mismatch')
                if result.status in {'COMPLETED','PARTIAL'}:
                    errors=self.validate_artifacts(task,result,workspace)
                    if errors:
                        result=ExecutionResult(task_id=task.task_id,status='FAILED',summary='artifact verification failed',error='; '.join(errors))
                with self.db.transaction() as conn:
                    self.transition(conn,task.task_id,result.status,result.error or '')
                    conn.execute('UPDATE tasks SET result=?,workspace=? WHERE id=?',(result.model_dump_json(),str(workspace),task.task_id))
            except Exception as exc:
                with self.db.transaction() as conn:
                    state=conn.execute('SELECT state FROM tasks WHERE id=?',(task.task_id,)).fetchone()['state']
                    if state in {'PREFLIGHT','RUNNING'}:
                        self.transition(conn,task.task_id,'BLOCKED' if state=='PREFLIGHT' else 'FAILED',type(exc).__name__)

