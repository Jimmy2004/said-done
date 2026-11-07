"""Version 1.0 contracts shared by every adapter. All times are UTC-aware."""
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

def utcnow():
    return datetime.now(timezone.utc)

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = '1.0'

class TranscriptSegment(Contract):
    segment_id: str = Field(min_length=1, max_length=200)
    timestamp_start: AwareDatetime
    timestamp_end: AwareDatetime
    text: str = Field(min_length=1, max_length=100000)
    source: str = Field(min_length=1)
    @model_validator(mode='after')
    def valid(self):
        self.text = self.text.strip()
        if not self.text or self.timestamp_end < self.timestamp_start:
            raise ValueError('empty text or reversed timestamps')
        return self

class TranscriptBatch(Contract):
    batch_id: str
    segments: list[TranscriptSegment] = Field(min_length=1)

class TaskState(StrEnum):
    DETECTED='DETECTED'
    CLASSIFIED='CLASSIFIED'
    SKIPPED='SKIPPED'
    QUEUED='QUEUED'
    PREFLIGHT='PREFLIGHT'
    BLOCKED='BLOCKED'
    RUNNING='RUNNING'
    COMPLETED='COMPLETED'
    PARTIAL='PARTIAL'
    FAILED='FAILED'
    CANCELLED='CANCELLED'

class TaskPacket(Contract):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: Literal['CREATE_DOCUMENT','CONVERT_FILE','WRITE_CODE','MODIFY_FILE']
    goal: str = Field(min_length=3)
    evidence_segment_ids: list[str] = Field(min_length=1)
    original_evidence: list[str] = Field(min_length=1)
    input_paths: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    historical_context: list[str] = Field(default_factory=list)
    user_facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    defaults: dict[str,str] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(min_length=1)
    forbidden_assumptions: list[str] = Field(default_factory=list)
    dedup_key: str = Field(min_length=1)

class TaskDecision(Contract):
    action: Literal['APPROVE','IGNORE','CANCEL','ALREADY_COMPLETED']
    reason: str
    task: TaskPacket | None = None
    target_dedup_key: str | None = None
    @model_validator(mode='after')
    def valid(self):
        if self.action == 'APPROVE' and self.task is None:
            raise ValueError('APPROVE requires task')
        if self.action != 'APPROVE' and self.task is not None:
            raise ValueError('only APPROVE may carry task')
        return self

class SemanticResponse(Contract):
    decisions: list[TaskDecision]

class PreflightResult(Contract):
    status: Literal['READY','BLOCKED']
    reasons: list[str] = Field(default_factory=list)
    checked_paths: list[str] = Field(default_factory=list)

class ExecutionResult(Contract):
    task_id: str
    status: Literal['COMPLETED','PARTIAL','FAILED','CANCELLED']
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None

