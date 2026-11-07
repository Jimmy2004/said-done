from pathlib import PureWindowsPath, PurePosixPath
from said_done.contracts import SemanticResponse, TranscriptBatch
from said_done.config import GateConfig

def validate_response(response: SemanticResponse | dict, batch: TranscriptBatch,
                      known_tasks: list[dict] | None = None,
                      gate: GateConfig | None = None) -> SemanticResponse:
    gate = gate or GateConfig()
    response=SemanticResponse.model_validate(response)
    ids={s.segment_id:s for s in batch.segments}
    if len(ids)!=len(batch.segments):
        raise ValueError('evidence segment IDs are ambiguous across sources')
    for d in response.decisions:
        if known_tasks is not None and d.action in {'CANCEL','ALREADY_COMPLETED'}:
            valid_keys={t['dedup_key'] for t in known_tasks}|{v.task.dedup_key for v in response.decisions if v.task}
            if d.target_dedup_key not in valid_keys:
                raise ValueError('control signal invents a task identity absent from trusted context')
        if d.action=='CANCEL' and not any(any(x in s.text for x in gate.cancel_keywords) for s in batch.segments):
            raise ValueError('cancellation lacks transcript evidence')
        if d.action=='ALREADY_COMPLETED' and not any(any(x in s.text for x in gate.completion_keywords) for s in batch.segments):
            raise ValueError('completion signal lacks transcript evidence')
        if d.action!='APPROVE':
            continue
        t=d.task
        if t.task_type not in gate.allowed_task_types:
            raise ValueError('task type is not allowed by gate policy')
        if not set(t.evidence_segment_ids).issubset(ids):
            raise ValueError('task references unknown evidence')
        evidence=[ids[sid].text for sid in t.evidence_segment_ids]
        if any(not quote.strip() or not any(quote in text for text in evidence) for quote in t.original_evidence):
            raise ValueError('task evidence is not an original transcript quote')
        # expected_outputs are relative to the workspace output/ directory;
        # models sometimes repeat that prefix, which would nest output/output/.
        t.expected_outputs=[_strip_output_prefix(name) for name in t.expected_outputs]
        for name in t.expected_outputs:
            wp=PureWindowsPath(name);pp=PurePosixPath(name)
            if wp.is_absolute() or wp.drive or pp.is_absolute() or '..' in wp.parts or '..' in pp.parts or ':' in name:
                raise ValueError('unsafe expected output path')
        if t.task_type in {'CONVERT_FILE','MODIFY_FILE'} and not t.input_paths:
            raise ValueError('file task requires explicit input paths')
    return response


def _strip_output_prefix(name: str) -> str:
    parts = name.strip().replace('\\', '/').split('/')
    while len(parts) > 1 and parts[0] in ('output', '.'):
        parts = parts[1:]
    return '/'.join(parts)
