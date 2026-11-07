import json
import logging
import time
from said_done.contracts import TranscriptBatch, SemanticResponse
from said_done.broker.lock import single_worker
from said_done.heartbeat.threshold import should_process
from said_done.storage.audit import ACCEPTED, ERROR, REJECTED, AuditEntry, source_refs
from said_done.broker.deduplication import provenance_fingerprint, task_fingerprint

class Scheduler:
    def __init__(self, settings, repository, semantic, broker, *,
                 poll_source=None, screening_only=False, poll_schedule=None, sources=None):
        self.settings,self.repository,self.semantic,self.broker=settings,repository,semantic,broker
        # A source poller gates the tick: it returns True only when the source
        # window is complete, False while more input may still arrive.
        self.sources=list(sources or [])
        self.poll_source=poll_source
        self.poll_schedule=poll_schedule
        # Screening mode stops after admission: approved tasks stay queued and
        # nothing is recovered or executed.
        self.screening_only=screening_only

    def tick(self, force=False):
        s=self.settings
        counts={'screened':0,'accepted':0,'rejected':0,'errors':0}
        with single_worker(s.data_dir/'worker.lock'):
            from said_done.ingestion.interface import SourceContext
            complete=True
            for source in self.sources:
                try:
                    result=source.poll(SourceContext(self.repository,source.id))
                except Exception:
                    raise RuntimeError('transcript source poll failed; input retained') from None
                self.repository.ingest(result.segments)
                complete=complete and result.complete
            if self.poll_source is not None:
                complete=bool(self.poll_source()) and complete
            if not complete:
                # The source window is incomplete: classifying now could decide
                # on a partial transcript, so nothing downstream runs.
                return {'polled':False,'screening_only':self.screening_only,**counts}
            if not self.screening_only:
                self.broker.recover()
            pending=self.repository.pending(s.screening.batch.max_batch_segments)
            if pending and (force or should_process(pending,s.screening.batch.min_content_chars,s.screening.batch.max_batch_wait_seconds, keywords=s.screening.batch.trigger_keywords)):
                self.repository.reserve_batch(pending)
            for row in self.repository.retry_batches():
                result=self._classify(row)
                for key in counts:
                    counts[key]+=result.get(key,0)
            if not self.screening_only:
                # Failed classification of a possible withdrawal must not allow
                # the still-queued task to run before that signal is resolved.
                if not self.repository.has_unclassified_control(keywords=s.screening.gate.cancel_keywords+s.screening.gate.completion_keywords):
                    self.broker.run_queued()
        return {'polled':True,'screening_only':self.screening_only,**counts}

    def _classify(self,row):
        """Classify one batch and audit the outcome in the same transaction."""
        batch=None
        try:
            batch=self.repository.screening_batch(row)
            response=SemanticResponse.model_validate(self.semantic.classify(batch))
            from said_done.semantic.validator import validate_response
            validate_response(response,batch,known_tasks=self.repository.semantic_context(batch),gate=self.settings.screening.gate)
            attempt=int(row['attempts'])+1
            with self.repository.db.transaction() as conn:
                started=time.time()
                self.broker.accept(conn,response,batch)
                entries=self._audit_entries(conn,response,batch,row['id'],attempt,started)
                self.repository.record_screening_audit(conn,entries)
                conn.execute("UPDATE batches SET status='CLASSIFIED',attempts=attempts+1,error=NULL WHERE id=?",(row['id'],))
                conn.execute('UPDATE segments SET classified=1 WHERE batch_id=?',(row['id'],))
            return self._counts(entries)
        except Exception as exc:
            self._fail_batch(row,batch,exc)
            return {'screened':1,'accepted':0,'rejected':0,'errors':1}

    def _audit_entries(self,conn,response,batch,batch_id,attempt,started):
        """One audit row per decision.

        The model's verdict and the broker's deduplication are kept apart: an
        approval stays ACCEPTED even when the task it named was already
        known, and the detail records that the existing task was kept.
        """
        model=self._model_name()
        if not response.decisions:
            # Screening succeeded but proposed no candidate at all.
            return [AuditEntry(batch_id=batch_id,outcome=REJECTED,action='NO_CANDIDATE',model=model,
                reason='screening returned no candidate task',
                source_refs=tuple(source_refs(batch,self._pending_segment_ids(conn,batch,batch_id))),
                attempt=attempt)]
        # A batch only carries its own segments; they are the evidence for a
        # decision without its own citation.
        pending_ids=self._pending_segment_ids(conn,batch,batch_id)
        entries=[]
        for decision in response.decisions:
            if decision.action=='APPROVE':
                task=decision.task
                refs=tuple(source_refs(batch,task.evidence_segment_ids))
                task_id,detail=self._resolve_approval(conn,task,batch,started)
                entries.append(AuditEntry(batch_id=batch_id,outcome=ACCEPTED,action=decision.action,
                    model=model,reason=decision.reason or None,detail=detail,source_refs=refs,
                    evidence_segment_ids=tuple(task.evidence_segment_ids),task_id=task_id,
                    linked_task_ids=(task_id,) if task_id else (),dedup_key=task.dedup_key,
                    attempt=attempt))
                continue
            linked=[]
            if decision.target_dedup_key:
                linked=[r['id'] for r in conn.execute('SELECT id FROM tasks WHERE dedup_key=?',(decision.target_dedup_key,))]
            entries.append(AuditEntry(batch_id=batch_id,outcome=REJECTED,action=decision.action,
                model=model,reason=decision.reason or None,source_refs=tuple(source_refs(batch,pending_ids)),
                task_id=linked[0] if linked else None,linked_task_ids=tuple(linked),
                target_dedup_key=decision.target_dedup_key,attempt=attempt))
        return entries

    def _pending_segment_ids(self,conn,batch,batch_id):
        """Segments this batch was meant to screen, without the context."""
        ids=set()
        for row in conn.execute('SELECT body FROM segments WHERE batch_id=? AND classified=0',(batch_id,)):
            try:
                ids.add(json.loads(row['body'])['segment_id'])
            except (ValueError,KeyError,TypeError):
                continue
        return [s.segment_id for s in batch.segments if s.segment_id in ids]

    def _resolve_approval(self,conn,task,batch,started):
        """The task an approval refers to, and whether it was admitted now.

        The broker suppresses a repeated intent, which is not a model
        rejection: the audit links the task that was kept.
        """
        provenance=provenance_fingerprint(task,batch)
        own=conn.execute('SELECT task_id FROM task_provenance WHERE fingerprint=? AND task_id=?',
                         (provenance,task.task_id)).fetchone()
        row=conn.execute('SELECT id,created FROM tasks WHERE id=?',(task.task_id,)).fetchone()
        if own and row and row['created']>=started:
            return task.task_id,None
        existing=conn.execute('SELECT task_id FROM task_provenance WHERE fingerprint=? ORDER BY rowid LIMIT 1',
                              (provenance,)).fetchone()
        if existing:
            return existing['task_id'],'duplicate: existing task retained'
        by_key=conn.execute('SELECT id FROM tasks WHERE dedup_key=?',(task.dedup_key,)).fetchone()
        if by_key:
            return by_key['id'],'duplicate: existing task retained'
        fingerprint=task_fingerprint(task.task_type,task.goal,task.input_paths,task.expected_outputs)
        by_fingerprint=conn.execute('SELECT id FROM tasks WHERE fingerprint=?',(fingerprint,)).fetchone()
        if by_fingerprint:
            return by_fingerprint['id'],'duplicate: existing task retained'
        return None,'approved; no task admitted or linked'

    def _fail_batch(self,row,batch,exc):
        """Record the failure and the retry bookkeeping together."""
        s=self.settings
        attempts=int(row['attempts'])+1
        status='EXHAUSTED' if attempts>=s.screening.gate.max_classification_attempts else 'RETRY'
        message=self._safe_error(exc)
        entry=AuditEntry(batch_id=row['id'],outcome=ERROR,model=self._model_name(),
                         error=message,
                         source_refs=tuple(source_refs(batch)) if batch is not None else (),
                         attempt=attempts)
        try:
            with self.repository.db.transaction() as conn:
                conn.execute('UPDATE batches SET status=?,attempts=?,next_retry=?,error=? WHERE id=?',
                 (status,attempts,time.time()+s.screening.gate.retry_delay_seconds,message,row['id']))
                self.repository.record_screening_audit(conn,[entry])
        except Exception:
            # A broken audit must not wedge the state machine; the failure stays
            # visible in the batch error column.
            logging.error('screening failure audit could not be stored')
            with self.repository.db.transaction() as conn:
                conn.execute('UPDATE batches SET status=?,attempts=?,next_retry=?,error=? WHERE id=?',
                 (status,attempts,time.time()+s.screening.gate.retry_delay_seconds,message,row['id']))

    def _safe_error(self,exc):
        """Error text that never echoes transcript content.

        MiniMax failures are sanitized by the adapter; any other exception
        (a pydantic validation error, for instance) can quote the transcript,
        so only its type is kept.
        """
        name=type(exc).__name__
        try:
            from said_done.semantic.minimax_adapter import MiniMaxError
        except Exception:
            return name
        if isinstance(exc,MiniMaxError):
            return f'{name}: {str(exc)[:500]}'
        return name

    def _counts(self,entries):
        counts={'screened':1,'accepted':0,'rejected':0,'errors':0}
        for entry in entries:
            if entry.outcome==ACCEPTED:
                counts['accepted']+=1
            elif entry.outcome==REJECTED:
                counts['rejected']+=1
        return counts

    def _model_name(self):
        model=getattr(self.semantic,'model',None)
        if isinstance(model,str) and model.strip():
            return model
        if self.settings.classifier.kind=='mock':
            return 'mock'
        return self.settings.classifier.model or 'unknown'

    def close(self):
        for source in self.sources:
            source.close()
        try:
            close = getattr(self.broker.executor, 'close', None)
            if close:
                close()
        finally:
            semantic_close=getattr(self.semantic,'close',None) or getattr(getattr(self.semantic,'client',None),'close',None)
            if semantic_close:
                semantic_close()

    def run(self, stop_event=None):
        import threading
        stop_event = stop_event or threading.Event()
        stopping = stop_event.is_set
        # Lifetime lock prevents two runners sharing the same runtime from
        # alternating ticks and mistakenly recovering each other's work.
        with single_worker(self.settings.data_dir/'runner.lock'):
            schedule = self.poll_schedule
            clock = time.time
            deadline = schedule.first_deadline() if schedule else clock()
            def wait_until(when):
                while not stopping() and clock() < when:
                    stop_event.wait(min(.2, max(0, when-clock())))
            while not stopping():
                wait_until(deadline - schedule.precheck_lead if schedule else deadline)
                if stopping():
                    break
                if schedule:
                    corrected = schedule.defer_before_poll(deadline)
                    if stopping():
                        break
                    if corrected is not None:
                        deadline = corrected
                        continue
                    wait_until(deadline)
                    if stopping():
                        break
                try:
                    self.tick()
                except Exception:
                    logging.error('heartbeat failed; pending data retained')
                if schedule:
                    deadline=schedule.next_deadline()
                else:
                    now=clock()
                    deadlines=[source.next_deadline(now) for source in self.sources]
                    deadline=min([now+self.settings.screening.batch.heartbeat_interval_seconds]+
                                 [d for d in deadlines if d is not None and d>now])
