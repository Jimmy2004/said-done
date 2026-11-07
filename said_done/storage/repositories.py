import hashlib
import json
import time
from uuid import uuid4
from said_done.contracts import ExecutionResult, TranscriptSegment, TranscriptBatch
from said_done.storage.audit import INSERT_SQL, query as audit_query, summary as audit_summary

def segment_key(segment):
    return hashlib.sha256((segment.source + '\0' + segment.segment_id).encode()).hexdigest()

class Repository:
    def __init__(self, db):
        self.db = db

    def ingest(self, segments):
        count = 0
        with self.db.transaction() as conn:
            for segment in segments:
                segment = TranscriptSegment.model_validate(segment)
                old = conn.execute('SELECT body FROM segments WHERE id=?',(segment_key(segment),)).fetchone()
                body = segment.model_dump_json()
                if old and old['body'] != body:
                    raise ValueError('source/segment_id collision with changed content')
                count += conn.execute('INSERT OR IGNORE INTO segments(id,body,received) VALUES(?,?,?)',
                                      (segment_key(segment),body,time.time())).rowcount
        return count

    def pending(self, limit=50):
        with self.db.connect() as conn:
            return [TranscriptSegment.model_validate_json(r['body']) for r in conn.execute(
                'SELECT body FROM segments WHERE classified=0 AND batch_id IS NULL ORDER BY received,id LIMIT ?', (limit,))]

    def reserve_batch(self, segments):
        """Reserve a batch that carries only the text it must screen.

        A batch holds exactly the newly unclassified segments: classified
        history is never replayed to the model, so a repeated tick cannot
        resend transcripts that were already screened.
        """
        batch = TranscriptBatch(batch_id=str(uuid4()),segments=segments)
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO batches(id,body,status) VALUES(?,?,?)',
                         (batch.batch_id,batch.model_dump_json(),'PENDING'))
            for seg in segments:
                conn.execute('UPDATE segments SET batch_id=? WHERE id=? AND classified=0 AND batch_id IS NULL',
                             (batch.batch_id,segment_key(seg)))
        return batch

    def batch_segment_keys(self, batch_id):
        """Segment keys a batch still owns and has not screened yet."""
        with self.db.connect() as conn:
            return {r['id'] for r in conn.execute(
                'SELECT id FROM segments WHERE batch_id=? AND classified=0',(batch_id,))}

    def screening_batch(self, row):
        """The batch a stored row screens, reduced to the segments it owns.

        Batches reserved before incremental screening started carry already
        classified transcripts as context. Replaying those would resend
        screened history to the model, so the batch is narrowed to its own
        segments and the stored body is corrected. The removed segments stay
        in the segments table; nothing is discarded.

        Fails closed: without an exact ownership match the stored body cannot
        be proven free of history, so it is never handed to the model.
        """
        batch=TranscriptBatch.model_validate_json(row['body'])
        owned=self.batch_segment_keys(row['id'])
        if not owned:
            raise ValueError('batch owns no unclassified segment; refusing to screen a body that may carry history')
        segments=[s for s in batch.segments if segment_key(s) in owned]
        if len(segments)!=len(owned) or {segment_key(s) for s in segments}!=owned:
            raise ValueError('batch body does not match the segments it owns; refusing to screen')
        if len(segments)==len(batch.segments):
            return batch
        narrowed=batch.model_copy(update={'segments':segments})
        with self.db.transaction() as conn:
            conn.execute('UPDATE batches SET body=? WHERE id=?',(narrowed.model_dump_json(),row['id']))
        return narrowed

    def retry_batches(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM batches WHERE status IN ('PENDING','RETRY') AND next_retry<=? ORDER BY rowid",(time.time(),)).fetchall()

    def requeue_classification(self):
        with self.db.transaction() as conn:
            return conn.execute("UPDATE batches SET status='RETRY',attempts=0,next_retry=0,error=NULL WHERE status='EXHAUSTED'").rowcount

    def status(self):
        with self.db.connect() as conn:
            return {name:[dict(r) for r in conn.execute(query)] for name,query in {
                'tasks':'SELECT id,state,error FROM tasks ORDER BY created',
                'batches':'SELECT id,status,attempts,error FROM batches',
                'pending':'SELECT COUNT(*) AS count FROM segments WHERE classified=0',
                'screening_audit':'SELECT outcome,COUNT(*) AS count FROM screening_audit GROUP BY outcome'}.items()}

    def task_result(self, task_id):
        """Read a locally persisted, validated execution result, if available."""
        with self.db.connect() as conn:
            row=conn.execute('SELECT result FROM tasks WHERE id=?',(task_id,)).fetchone()
        return ExecutionResult.model_validate_json(row['result']) if row and row['result'] else None

    def record_screening_audit(self, conn, entries):
        """Insert screening audit rows inside the caller's transaction."""
        rows=[entry.row() for entry in entries]
        if rows:
            conn.executemany(INSERT_SQL, rows)
        return len(rows)

    def audit_records(self, limit=100, outcome=None, action=None, batch_id=None, task_id=None,
                      since=None, until=None):
        """Durable screening audit rows, newest first.

        outcome is one of ACCEPTED, REJECTED or ERROR; a classification failure is
        never reported as a rejection.
        """
        with self.db.connect() as conn:
            return audit_query(conn, limit=limit, outcome=outcome, action=action,
                               batch_id=batch_id, task_id=task_id, since=since, until=until)

    def audit_summary(self, since=None, until=None):
        """Screening audit counts by outcome and by decision action."""
        with self.db.connect() as conn:
            return audit_summary(conn, since=since, until=until)

    def semantic_context(self,batch=None):
        """Known task identities, not transcript history.

        Cancellation and deduplication need the persisted task identity, so
        the task id, dedup key, goal and state are exposed. The original
        transcript quotes behind a past task are never retransmitted.
        """
        with self.db.connect() as conn:
            rows=conn.execute('SELECT body,state FROM tasks ORDER BY created DESC LIMIT 20').fetchall()
        result=[]
        for row in rows:
            task=json.loads(row['body'])
            result.append({'task_id':task['task_id'],'dedup_key':task['dedup_key'],'goal':task['goal'],
                           'state':row['state']})
        return result

    def has_unclassified_control(self, keywords=None):
        with self.db.connect() as conn:
            rows=conn.execute('SELECT body FROM segments WHERE classified=0').fetchall()
        signals=tuple(keywords) if keywords is not None else ('取消','撤销','已经完成','已完成','已经做完','做完了','cancel')
        return any(any(signal.casefold() in json.loads(row['body'])['text'].casefold() for signal in signals) for row in rows)

    def list_tasks(self, state=None, limit=50) -> list[dict]:
        """List task metadata without returning transcript evidence."""
        from said_done.contracts import TaskState
        limit=max(1,min(int(limit),500))
        where=' WHERE state=?' if state is not None else ''
        params=(str(TaskState(state)),limit) if state is not None else (limit,)
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT id,state,workspace,error,created FROM tasks'+where+' ORDER BY created DESC LIMIT ?',params)]

    def cancel_task(self, task_id) -> dict:
        """Cancel queued/preflight work atomically through the state machine."""
        from said_done.broker.state_machine import check_transition
        with self.db.transaction() as conn:
            row=conn.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()
            if row is None:
                return {'task_id':task_id,'cancelled':False,'reason':'not found'}
            if row['state'] not in {'QUEUED','PREFLIGHT'}:
                return {'task_id':task_id,'cancelled':False,'state':row['state']}
            check_transition(row['state'],'CANCELLED')
            conn.execute("UPDATE tasks SET state='CANCELLED',error=NULL WHERE id=?",(task_id,))
            conn.execute('INSERT INTO events(task_id,state,detail,created) VALUES(?,?,?,?)',
                         (task_id,'CANCELLED','cancelled by user',time.time()))
            return {'task_id':task_id,'cancelled':True,'state':'CANCELLED'}
