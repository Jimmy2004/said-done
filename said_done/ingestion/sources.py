"""Built-in transcript plugins. Polls persist before advancing checkpoints."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from said_done.contracts import TranscriptSegment
from .interface import PollResult, SourceContext


class ManualOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')


class FolderOptions(ManualOptions):
    path: str


class McpOptions(ManualOptions):
    command: list[str] = Field(min_length=1)
    device: str = ''
    transcript_tool: str = 'get_transcripts'
    status_tool: str | None = 'sync_status'
    poll_interval_seconds: float = Field(default=900,gt=0)
    preset: str | None = None


class ManualSource:
    kind='manual'
    def __init__(self, source_id, options=None, repository=None):
        self.id=source_id
    def poll(self, ctx):
        return PollResult()
    def next_deadline(self, now):
        return None
    def status(self):
        return {'id':self.id,'kind':self.kind}
    def close(self):
        pass


class FolderSource(ManualSource):
    kind='folder'
    def __init__(self, source_id, options, repository=None):
        super().__init__(source_id)
        self.path=Path(FolderOptions.model_validate(options).path).expanduser()

    def poll(self, ctx):
        segments=[]
        self.path.mkdir(parents=True,exist_ok=True)
        processed=self.path/'processed'
        if processed.is_symlink():
            raise ValueError('processed directory must not be a symbolic link')
        processed.mkdir(exist_ok=True)
        for path in sorted(self.path.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {'.txt','.json'}:
                continue
            payload=path.read_bytes()
            digest=hashlib.sha256(str(path.resolve()).encode()+b'\0'+payload).hexdigest()
            if not ctx.get(digest,False):
                if path.suffix.lower()=='.txt':
                    moment=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)
                    batch=[TranscriptSegment(segment_id=digest,timestamp_start=moment,timestamp_end=moment,
                                             text=payload.decode('utf-8'),source=self.id)]
                else:
                    data=json.loads(payload)
                    batch=[TranscriptSegment.model_validate(x) for x in (data if isinstance(data,list) else [data])]
                batch=[s.model_copy(update={'segment_id':f'{self.id}:{s.segment_id}', 'source':self.id}) for s in batch]
                # Revalidate namespaced IDs rather than bypassing contract limits.
                batch=[TranscriptSegment.model_validate(s.model_dump()) for s in batch]
                ctx.ingest(batch)
                ctx.set(digest,True)
                segments.extend(batch)
            target=processed/path.name
            if target.exists():
                target=processed/(digest+path.suffix)
            path.replace(target)
        return PollResult(segments)


class McpSource(ManualSource):
    kind='mcp-stdio'
    def __init__(self, source_id, options, repository):
        from .cloud_mcp_client import CloudTranscriptClient
        from .cloud_poll import CloudTranscriptPoll
        from .cloud_schedule import CloudPollSchedule
        super().__init__(source_id)
        self.options=McpOptions.model_validate(options)
        o=self.options
        self.client=CloudTranscriptClient(o.command)
        self.legacy=o.preset=='cloud-transcript-sync'
        self.poller=CloudTranscriptPoll(repository,self.client,o.device,
            source_id=None if self.legacy else self.id,transcript_tool=o.transcript_tool)
        self.schedule=CloudPollSchedule(self.client,o.device,o.poll_interval_seconds,status_tool=o.status_tool)
        self._deadline=None
        self._complete=True

    def poll(self, ctx):
        self._complete=self.poller()
        ctx.set('complete',self._complete)
        return PollResult(complete=self._complete)

    def next_deadline(self, now):
        if self.schedule:
            if self._deadline is None:
                self._deadline=self.schedule.first_deadline()
            elif self._deadline<=now:
                self._deadline=self.schedule.next_deadline()
            return self._deadline
        return now+self.options.poll_interval_seconds

    def status(self):
        return {**super().status(),'complete':self._complete,'preset':self.options.preset}
