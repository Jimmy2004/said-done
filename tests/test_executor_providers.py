import json
import shutil
import sys
from pathlib import Path
import pytest
from said_done.config import Settings
from said_done.contracts import TaskPacket
from said_done.executor.registry import build_executor, list_executors, executor_info


def make(tmp_path,kind,options=None,timeout=2):
    fake=tmp_path/'fake'
    fake.write_text('#!'+sys.executable+'\n'+Path(__file__).with_name('fake_executor_provider.py').read_text())
    fake.chmod(0o700)
    settings=Settings(executor={'kind':kind,'executable':str(fake),'model':'provider/test',
        'effort':'low','timeout_seconds':timeout,'options':options or {}})
    return build_executor(settings),fake


def task():
    return TaskPacket(task_type='CREATE_DOCUMENT',goal='XRD film characterization report outline',
        evidence_segment_ids=['s'],original_evidence=['PRIVATE_PROMPT'],requirements=['XRD film characterization report outline'],
        expected_outputs=['report.txt'],dedup_key='xrd')


@pytest.mark.parametrize('kind',['claude-code','opencode','pi','custom-command','dsh'])
def test_cli_roundtrip(tmp_path,kind):
    options={}
    if kind in {'custom-command','dsh'}:
        options={'argv':[sys.executable,str(Path(__file__).with_name('fake_executor_provider.py').resolve()),'--stdin'],
                 'prompt_via':'stdin','command_confirmed':True}
    ex,_=make(tmp_path,kind,options)
    t=task()
    ex.create_task(t,tmp_path)
    result=ex.execute(t,tmp_path)
    assert result.status=='COMPLETED',result
    assert result.summary=={'claude-code':'claude done','opencode':'opencode done'}.get(kind,'command done')
    args=json.loads((tmp_path/'argv.json').read_text())
    if kind=='claude-code':
        assert args[:5]==['-p','--output-format','stream-json','--verbose','--permission-mode']
        assert 'Bash' not in args[args.index('--allowedTools')+1]
        assert '--effort' in args and '--model' in args
    if kind=='opencode':
        assert args[:4]==['run','--format','json','--auto']
        assert task().goal in args[-1]
    if kind=='pi':
        assert args[0]=='-p' and '--model' in args
        assert ex.verification=='experimental-unverified'
    assert ex.get_result(t.task_id)==result


@pytest.mark.parametrize('mode',['timeout','error','overflow'])
def test_process_limits_and_sanitization(tmp_path,monkeypatch,mode):
    ex,_=make(tmp_path,'claude-code',timeout=.2 if mode=='timeout' else 3)
    monkeypatch.setenv('FAKE_EXECUTOR_MODE',mode)
    t=task()
    ex.create_task(t,tmp_path)
    result=ex.execute(t,tmp_path)
    assert result.status=='FAILED'
    assert 'PRIVATE_PROMPT' not in result.model_dump_json()
    assert not ex._processes


def test_custom_templates_and_dsh_confirmation(tmp_path):
    with pytest.raises(ValueError,match='user-confirmed'):
        make(tmp_path,'dsh',{'argv':['dsh','{prompt}'],'prompt_via':'arg'})
    ex,_=make(tmp_path,'custom-command',{'argv':['tool','{workspace}','{prompt_file}','{model}','{effort}'],'prompt_via':'file'})
    assert ex._build_command(tmp_path,'secret','p.txt')==['tool',str(tmp_path),'p.txt','provider/test','low']
    with pytest.raises(ValueError,match='placeholder'):
        make(tmp_path,'custom-command',{'argv':['tool','{secret}']})


def test_discovery_offline(tmp_path,monkeypatch):
    _,fake=make(tmp_path,'pi')
    monkeypatch.setattr(shutil,'which',lambda name:str(fake))
    info=executor_info('pi')
    assert info['version']=='1.2.3'
    assert info['needs_user_verification'] and info['sandbox']=='none'
    monkeypatch.setattr(shutil,'which',lambda name:None)
    all_kinds={r['kind'] for r in list_executors()}
    assert {'host','claude-desktop','chatgpt-app','codex-cli','dsh'}<=all_kinds


def test_structured_provider_error_is_reported_without_message(tmp_path,monkeypatch):
    ex,_=make(tmp_path,'opencode',timeout=3)
    monkeypatch.setenv('FAKE_EXECUTOR_MODE','event-error')
    t=task()
    ex.create_task(t,tmp_path)
    result=ex.execute(t,tmp_path)
    assert result.status=='FAILED'
    assert result.error=="opencode reported error: provider.free-limit; set executor.model to a connected provider's model"
    assert 'PRIVATE_PROMPT' not in result.model_dump_json()
