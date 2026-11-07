from said_done.contracts import TaskState as S
TRANSITIONS = {
 S.DETECTED:{S.CLASSIFIED,S.SKIPPED}, S.CLASSIFIED:{S.QUEUED,S.SKIPPED},
 S.QUEUED:{S.PREFLIGHT,S.CANCELLED}, S.PREFLIGHT:{S.BLOCKED,S.RUNNING,S.CANCELLED},
 S.RUNNING:{S.COMPLETED,S.PARTIAL,S.FAILED,S.CANCELLED},
}
def check_transition(old: str, new: str):
    if S(new) not in TRANSITIONS.get(S(old),set()):
        raise ValueError(f'illegal task transition {old} -> {new}')
