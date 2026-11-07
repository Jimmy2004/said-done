import csv
import json
import sys
import zipfile
from pathlib import Path
from dataclasses import replace
import pytest
from said_done.contracts import *
from said_done.executor.config import *
from said_done.executor.preflight import run_preflight
from said_done.executor.mock_executor import MockExecutor
from said_done.executor.artifact_validator import validate_artifacts
from said_done.executor.codex_adapter import CodexAppServerExecutor,CodexAppServerTimeout,CodexAppServerError
from said_done.executor.cli_adapter import CodexCliExecutor

def task(**kwargs):
    v=dict(task_type='CREATE_DOCUMENT',goal='XRD thin film characterization report outline',evidence_segment_ids=['s'],original_evidence=['I need an XRD report outline'],user_facts=['XRD thin film characterization report outline'],expected_outputs=['outline.docx'],dedup_key='xrd')
    v.update(kwargs);return TaskPacket(**v)

def test_create_no_input_and_real_docx(tmp_path):
    ex=MockExecutor();t=task();ex.create_task(t,tmp_path)
    before=list(tmp_path.iterdir());assert ex.preflight(t,tmp_path).status=='READY'
    assert list(tmp_path.iterdir())==before
    result=ex.execute(t,tmp_path)
    assert result.status=='COMPLETED'
    assert not validate_artifacts(t,result,tmp_path)
    assert zipfile.is_zipfile(result.artifacts[0])
    assert ex.get_result(t.task_id)==result

def test_missing_file_no_effects(tmp_path):
    t=task(task_type='CONVERT_FILE',input_paths=[str(tmp_path/'absent.xlsx')],expected_outputs=['a.csv'])
    ex=MockExecutor();before=list(tmp_path.iterdir())
    assert ex.preflight(t,tmp_path).status=='BLOCKED'
    assert ex.execute(t,tmp_path).status=='FAILED'
    assert list(tmp_path.iterdir())==before

def test_duplicate_name_is_blocked(tmp_path):
    for d in ['one','two']:
        p=tmp_path/d;p.mkdir();(p/'A.xlsx').write_bytes(b'not used')
    t=task(task_type='CONVERT_FILE',input_paths=['A.xlsx'],expected_outputs=['a.csv'])
    result=run_preflight(t,tmp_path/'work',PreflightConfig(readonly_roots=(tmp_path,)))
    assert result.status=='BLOCKED'
    assert any('multiple' in r or 'ambiguous' in r or 'matches' in r for r in result.reasons)

def test_vague_goal_dependencies_permissions(tmp_path,monkeypatch):
    assert run_preflight(task(goal='do it'),tmp_path).status=='BLOCKED'
    assert run_preflight(task(),tmp_path,PreflightConfig(required_modules=('module_that_does_not_exist_xyz',))).status=='BLOCKED'
    import said_done.executor.preflight as pf
    monkeypatch.setattr(pf.os,'access',lambda *args:False)
    assert run_preflight(task(),tmp_path).status=='BLOCKED'

def test_scope_and_budget(tmp_path):
    assert run_preflight(task(expected_outputs=['../escape.docx']),tmp_path).status=='BLOCKED'
    assert run_preflight(task(),tmp_path,PreflightConfig(max_tool_calls=1)).status=='BLOCKED'
    external=tmp_path/'external';external.mkdir();(external/'A.xlsx').write_bytes(b'x')
    assert run_preflight(task(task_type='CONVERT_FILE',input_paths=[str(external/'A.xlsx')],expected_outputs=['a.csv']),tmp_path/'work').status=='BLOCKED'

def test_no_basename_substitute_for_missing_explicit_path(tmp_path):
    source=tmp_path/'A.xlsx';make_xlsx(source)
    t=task(task_type='CONVERT_FILE',input_paths=[str(tmp_path/'missing'/'A.xlsx')],expected_outputs=['A.csv'])
    assert run_preflight(t,tmp_path/'work',PreflightConfig(readonly_roots=(tmp_path,))).status=='BLOCKED'

def test_file_type_and_wildcard_rejected(tmp_path):
    bad=tmp_path/'bad.xlsx';bad.write_text('not Excel')
    t=task(task_type='CONVERT_FILE',input_paths=[str(bad)],expected_outputs=['A.csv'])
    assert run_preflight(t,tmp_path).status=='BLOCKED'
    assert run_preflight(t.model_copy(update={'input_paths':['**/*.xlsx']}),tmp_path).status=='BLOCKED'

def make_xlsx(path):
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>sample</t></is></c><c r="B1" t="inlineStr"><is><t>value</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>first</t></is></c><c r="B2"><v>42</v></c></row></sheetData></worksheet>')

def test_xlsx_conversion_preserves_input(tmp_path):
    source=tmp_path/'A.xlsx';make_xlsx(source);before=source.read_bytes()
    workspace=tmp_path/'work';workspace.mkdir()
    t=task(task_type='CONVERT_FILE',goal='Convert A.xlsx data to CSV',input_paths=[str(source)],expected_outputs=['A.csv'])
    ex=MockExecutor(preflight_config=PreflightConfig(readonly_roots=(tmp_path,)))
    assert ex.preflight(t,workspace).status=='READY'
    result=ex.execute(t,workspace)
    assert result.status=='COMPLETED',result.error
    rows=list(csv.reader(Path(result.artifacts[0]).open(encoding='utf-8-sig')))
    assert rows==[['sample','value'],['first','42']]
    assert source.read_bytes()==before

def test_artifact_omission_and_corruption_rejected(tmp_path):
    ex=MockExecutor();t=task();result=ex.execute(t,tmp_path)
    result.artifacts=[]
    assert validate_artifacts(t,result,tmp_path)
    out=tmp_path/'output'/'outline.docx';out.write_text('fake format content'*20)
    result.artifacts=[str(out)]
    assert validate_artifacts(t,result,tmp_path)

def test_no_input_file_can_be_delivered_as_output(tmp_path):
    ex=MockExecutor();t=task();result=ex.execute(t,tmp_path)
    input_file=tmp_path/'inputs'/'secret.txt';input_file.parent.mkdir(exist_ok=True);input_file.write_text('not an output')
    result.artifacts.append(str(input_file))
    assert any('outside' in p for p in validate_artifacts(t,result,tmp_path))

def fake_config(**kw):
    return AppServerConfig(executable=sys.executable,args=(str(Path(__file__).with_name('fake_app_server.py')),),startup_timeout_seconds=5,request_timeout_seconds=5,turn_timeout_seconds=5,**kw)

def test_appserver_synthetic_roundtrip(tmp_path):
    t=task();ex=CodexAppServerExecutor(config=fake_config())
    try:
        ex.create_task(t,tmp_path)
        assert ex.preflight(t,tmp_path).status=='READY'
        result=ex.execute(t,tmp_path)
        assert result.status=='COMPLETED',result.error
        assert not validate_artifacts(t,result,tmp_path)
        assert ex.get_result(t.task_id)==result
    finally:ex.close()

def test_appserver_failed_probe_no_outputs(tmp_path):
    (tmp_path/'probe_should_fail').touch();t=task();ex=CodexAppServerExecutor(config=fake_config())
    try:
        ex.create_task(t,tmp_path)
        assert ex.preflight(t,tmp_path).status=='BLOCKED'
        assert ex.execute(t,tmp_path).status=='FAILED'
        assert not (tmp_path/'output').exists()
    finally:ex.close()

def test_cli_synthetic_roundtrip(tmp_path):
    t=task();ex=CodexCliExecutor(config=CliExecutorConfig(executable=sys.executable,base_args=(str(Path(__file__).with_name('fake_codex_cli.py')),)))
    ex.create_task(t,tmp_path);assert ex.preflight(t,tmp_path).status=='READY'
    result=ex.execute(t,tmp_path)
    assert result.status=='COMPLETED',result.error

def test_appserver_local_only_is_blocked(tmp_path):
    ex=CodexAppServerExecutor(config=fake_config(server_preflight=False))
    assert ex.preflight(task(),tmp_path).status=='BLOCKED'

def test_appserver_unknown_status_never_succeeds(tmp_path):
    (tmp_path/'unknown-status').touch();t=task();ex=CodexAppServerExecutor(config=fake_config())
    try:
        ex.create_task(t,tmp_path);assert ex.preflight(t,tmp_path).status=='READY'
        assert ex.execute(t,tmp_path).status=='FAILED'
    finally:ex.close()

def test_appserver_timeout_stops_process_before_return(tmp_path):
    (tmp_path/'hang-turn').touch();t=task()
    ex=CodexAppServerExecutor(config=replace(fake_config(),turn_timeout_seconds=.15,interrupt_grace_seconds=.5))
    try:
        ex.create_task(t,tmp_path);assert ex.preflight(t,tmp_path).status=='READY'
        client=ex._client
        result=ex.execute(t,tmp_path)
        assert result.status=='FAILED'
        assert not client.running
        assert 'terminated' in result.error
    finally:ex.close()

def test_cli_timeout_stops_child(tmp_path):
    t=task(goal='HANG XRD characterization outline')
    ex=CodexCliExecutor(config=CliExecutorConfig(executable=sys.executable,base_args=(str(Path(__file__).with_name('fake_codex_cli.py')),),timeout_seconds=.2))
    ex.create_task(t,tmp_path)
    result=ex.execute(t,tmp_path)
    assert result.status=='FAILED'
    assert ex._process is None

def test_fake_pdf_cannot_pass_artifact_validation(tmp_path):
    p=tmp_path/'output';p.mkdir();f=p/'outline.pdf'
    f.write_bytes(b'%PDF-1.4\nfake report\n%%EOF\n')
    t=task(expected_outputs=['outline.pdf'])
    r=ExecutionResult(task_id=t.task_id,status='COMPLETED',summary='claims success',artifacts=[str(f)])
    assert validate_artifacts(t,r,tmp_path)

def test_appserver_is_restarted_for_each_workspace_boundary(tmp_path):
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    ex=CodexAppServerExecutor(config=fake_config())
    try:
        ex.create_task(task(task_id='one'),first)
        old=ex._client
        assert old.startup_cwd==first.resolve()
        ex.create_task(task(task_id='two'),second)
        assert not old.running
        assert ex._client.startup_cwd==second.resolve()
    finally:ex.close()

def test_running_appserver_cannot_be_retargeted_to_another_workspace(tmp_path):
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    ex=CodexAppServerExecutor(config=fake_config())
    try:
        ex.create_task(task(task_id='one'),first)
        ex._sessions['one']['status']='RUNNING'
        old=ex._client
        with pytest.raises(CodexAppServerError):ex.create_task(task(task_id='two'),second)
        assert old.running and ex._client is old
    finally:ex.close()


def test_codex_cli_never_inherits_stdin(tmp_path, monkeypatch):
    """`codex exec` blocks on a piped stdin; under MCP stdin is the JSON-RPC channel."""
    import subprocess
    from said_done.executor import cli_adapter
    from said_done.executor.config import CliExecutorConfig
    captured = {}
    class Stop(Exception): pass
    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        raise Stop
    monkeypatch.setattr(cli_adapter.subprocess, 'Popen', fake_popen)
    ex = cli_adapter.CodexCliExecutor(config=CliExecutorConfig(executable='codex'),
                                      preflight_config=PreflightConfig(readonly_roots=(tmp_path,)))
    t = task(task_type='WRITE_CODE', goal='Write a hello world script', expected_outputs=['hello.py'])
    workspace = tmp_path / 'work'; workspace.mkdir()
    ex.create_task(t, workspace)
    try:
        ex.execute(t, workspace)
    except Stop:
        pass
    assert captured.get('stdin') is subprocess.DEVNULL
