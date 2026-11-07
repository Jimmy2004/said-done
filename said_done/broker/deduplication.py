"""Adapter-neutral stable identity helper; no model calls or fuzzy guesses."""
import hashlib
import json
import unicodedata

def task_fingerprint(task_type: str, goal: str, input_paths: list[str], expected_outputs: list[str]) -> str:
    def normalize(value):
        return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())
    body=[task_type,normalize(goal),sorted(normalize(p) for p in input_paths),sorted(normalize(p) for p in expected_outputs)]
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def provenance_fingerprint(task, batch=None):
    """Identity from user evidence, independent of model wording and filenames.

    An exact repeated request is not a new execution just because a model
    paraphrases its goal. This deliberately does not claim fuzzy matching.
    """
    evidence=task.original_evidence
    if batch is not None:
        ids=set(task.evidence_segment_ids)
        evidence=[s.text for s in batch.segments if s.segment_id in ids]
    texts=sorted({' '.join(unicodedata.normalize('NFKC',t).casefold().split()) for t in evidence})
    formats=sorted({p.replace('\\','/').rsplit('.',1)[-1].casefold() for p in task.expected_outputs})
    body=[task.task_type,texts,formats]
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
