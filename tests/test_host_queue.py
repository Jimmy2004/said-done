"""Host-pull lifecycle, leases, recovery, and admission boundaries."""
import time
from pathlib import Path
import pytest
from said_done.config import Settings
from said_done.contracts import TaskPacket, SemanticResponse, TaskDecision
from said_done.runtime import create_runtime
from said_done.broker.host_queue import claim_next_task, complete_task, release_task


def runtime(tmp_path):
    return create_runtime(Settings(data_dir=tmp_path,executor={'kind':'host'}))


def enqueue(rt, suffix='one'):
    task=TaskPacket(task_type='CREATE_DOCUMENT',goal='XRD film characterization report outline',
                    original_evidence=['XRD film characterization report outline'],evidence_segment_ids=['s'],
                    requirements=['XRD film characterization report outline'],expected_outputs=['report.txt'],dedup_key=suffix)
    with rt.repository.db.transaction() as conn:
        rt.broker.accept(conn,SemanticResponse(decisions=[TaskDecision(action='APPROVE',reason='requested',task=task)]))
    return task


def test_host_complete_and_does_not_block_queue(tmp_path):
    rt=runtime(tmp_path)
    one=enqueue(rt)
    rt.broker.run_queued()
    rt.broker.run_queued()
    claimed=claim_next_task(rt.repository)
    assert claimed['task_id']==one.task_id
    assert claim_next_task(rt.repository) is None
    output=Path(claimed['workspace'])/'output/report.txt'
    output.write_text('XRD film characterization report outline. '*10)
    rt.broker.recover()
    assert rt.repository.list_tasks()[0]['state']=='RUNNING'
    result=complete_task(rt.repository,one.task_id,'done')
    assert result['status']=='COMPLETED'
    assert rt.repository.task_result(one.task_id).artifacts==[str(output)]
    assert claim_next_task(rt.repository) is None


def test_lease_expiry_release_and_restart(tmp_path):
    rt=runtime(tmp_path)
    task=enqueue(rt)
    rt.broker.run_queued()
    assert claim_next_task(rt.repository)['task_id']==task.task_id
    assert release_task(rt.repository,task.task_id)['released']
    claim_next_task(rt.repository)
    with rt.repository.db.transaction() as conn:
        conn.execute('UPDATE host_tasks SET lease_until=?',(time.time()-1,))
    with pytest.raises(ValueError,match='active lease'):
        complete_task(rt.repository,task.task_id,'too late')
    other=runtime(tmp_path)
    other.broker.recover()
    other.broker.recover()
    assert other.repository.list_tasks()[0]['state']=='RUNNING'
    with other.repository.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE detail LIKE 'host lease expired%'").fetchone()[0]==1
    assert claim_next_task(other.repository)['task_id']==task.task_id


def test_cannot_bypass_artifacts(tmp_path):
    rt=runtime(tmp_path)
    task=enqueue(rt)
    rt.broker.run_queued()
    claim_next_task(rt.repository)
    result=complete_task(rt.repository,task.task_id,'SECRET_CONTENT',validate=False)
    assert result['status']=='FAILED'
    assert 'SECRET_CONTENT' not in str(result)


def test_cancel_state_machine_and_list(tmp_path):
    rt=runtime(tmp_path)
    task=enqueue(rt)
    assert rt.repository.list_tasks(state='QUEUED')[0]['id']==task.task_id
    assert rt.repository.cancel_task(task.task_id)['cancelled']
    assert not rt.repository.cancel_task(task.task_id)['cancelled']
    assert not rt.repository.cancel_task('absent')['cancelled']
    assert not rt.repository.list_tasks(state='RUNNING')
    assert 'original_evidence' not in str(rt.repository.list_tasks())


@pytest.mark.parametrize('lease',[0,-1,float('inf'),float('nan')])
def test_invalid_lease(tmp_path,lease):
    with pytest.raises(ValueError):
        claim_next_task(runtime(tmp_path).repository,lease)
