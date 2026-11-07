import json
from datetime import timedelta
from pathlib import Path
import httpx
import pytest
from said_done.contracts import *
from said_done.semantic.mock_adapter import MockSemanticAdapter
from said_done.semantic.minimax_adapter import MiniMaxAdapter,MiniMaxError
from said_done.semantic.validator import validate_response

def batch(*texts):
    now=utcnow()
    return TranscriptBatch(batch_id='b',segments=[TranscriptSegment(segment_id=str(i),timestamp_start=now+timedelta(seconds=i),timestamp_end=now+timedelta(seconds=i+1),text=t,source='mock') for i,t in enumerate(texts)])

@pytest.mark.parametrize('text,accepted',[
 ('我要整理这个 Excel',False),('我要把桌面 A.xlsx 转成 CSV',True),
 ('我需要一个 XRD 原理报告大纲',True),('我去查一下天气',False),
 ('这个报告已经做完了',False),('假设我要做个网站',False),
 ('朋友说我要写一个 XRD 报告大纲',False),('我需要一个报告大纲',False),
 ('我需要一个半导体封装可靠性报告的大纲',True),
])
def test_gate_examples(text,accepted):
    b=batch(text);r=MockSemanticAdapter().classify(b)
    validate_response(r,b)
    assert any(d.action=='APPROVE' for d in r.decisions)==accepted

def test_create_default_and_evidence():
    b=batch('我需要一个 XRD 原理报告大纲');r=MockSemanticAdapter().classify(b);t=r.decisions[0].task
    assert t.task_type=='CREATE_DOCUMENT' and not t.input_paths
    assert t.defaults['format']=='docx'
    assert t.original_evidence==[b.segments[0].text]

def test_context_and_dedup():
    r=MockSemanticAdapter().classify(batch('我需要一个报告大纲','主题是 XRD 原理'))
    assert r.decisions[0].action=='APPROVE'
    assert r.decisions[0].task.evidence_segment_ids==['0','1']
    r=MockSemanticAdapter().classify(batch('我需要一个 XRD 原理报告大纲','我需要一个 XRD 原理报告大纲'))
    assert len([d for d in r.decisions if d.action=='APPROVE'])==1

def test_cancel_unique():
    r=MockSemanticAdapter().classify(batch('我需要一个 XRD 原理报告大纲','取消 XRD 原理报告大纲'))
    assert r.decisions[-1].action=='CANCEL'
    assert r.decisions[-1].target_dedup_key==r.decisions[0].task.dedup_key

def test_evidence_and_path_validation():
    b=batch('我需要一个 XRD 原理报告大纲');r=MockSemanticAdapter().classify(b)
    r.decisions[0].task.original_evidence=['invented evidence']
    with pytest.raises(ValueError):validate_response(r,b)
    r=MockSemanticAdapter().classify(b);r.decisions[0].task.expected_outputs=['../bad.docx']
    with pytest.raises(ValueError):validate_response(r,b)

def test_native_http_json_schema_and_payload():
    b=batch('我需要一个 XRD 原理报告大纲');expected=MockSemanticAdapter().classify(b)
    calls=[]
    def transport(req):
        calls.append(json.loads(req.content))
        assert req.url.path=='/v1/text/chatcompletion_v2'
        return httpx.Response(200,json={'base_resp':{'status_code':0},'choices':[{'message':{'content':expected.model_dump_json()}}]})
    gate=MiniMaxAdapter(api_key='test-only',model='MiniMax-M2.7',client=httpx.Client(transport=httpx.MockTransport(transport)),use_response_format=True,sleep=lambda _:None)
    assert gate.classify(b)==expected
    assert calls[0]['response_format']['type']=='json_schema'


@pytest.mark.parametrize('model,effort',[(None,'low'),('MiniMax-M3',None)])
def test_m3_thinking_stays_enabled_and_flash_uses_lowest_effort(model,effort):
    b=batch('假设我要做个网站')
    expected=MockSemanticAdapter().classify(b)
    def transport(req):
        body=json.loads(req.content)
        assert req.url.path=='/v1/chat/completions'
        assert body['model']==(model or 'MiniMax-M3.1-Flash')
        assert body['thinking']=={'type':'adaptive'}
        assert body.get('reasoning_effort')==effort
        return httpx.Response(200,json={'choices':[{'message':{'content':expected.model_dump_json()}}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        options={'model':model} if model else {}
        assert MiniMaxAdapter(api_key='test-only',client=client,**options).classify(b)==expected

@pytest.mark.parametrize('case',['malformed','business','network'])
def test_http_finite_retry_and_failclosed(case):
    count=[]
    def transport(req):
        count.append(1)
        if case=='network':raise httpx.ConnectError('offline',request=req)
        if case=='business':return httpx.Response(200,json={'base_resp':{'status_code':1002}})
        return httpx.Response(200,json={'choices':[{'message':{'content':'not JSON'}}]})
    gate=MiniMaxAdapter(api_key='test-only',max_attempts=2,sleep=lambda _:None,client=httpx.Client(transport=httpx.MockTransport(transport)))
    with pytest.raises(MiniMaxError):gate.classify(batch('我需要一个 XRD 原理报告大纲'))
    assert len(count)==2

def test_auth_not_retried():
    calls=[]
    def transport(req):calls.append(1);return httpx.Response(401)
    gate=MiniMaxAdapter(api_key='test-only',sleep=lambda _:None,client=httpx.Client(transport=httpx.MockTransport(transport)))
    with pytest.raises(MiniMaxError):gate.classify(batch('我需要一个 XRD 原理报告大纲'))
    assert len(calls)==1

def test_persisted_control_identity_is_transmitted_and_validated():
    b=batch('取消 XRD 原理报告大纲')
    known=[{'task_id':'stable-task','dedup_key':'persisted-identity-42','goal':'XRD 原理报告大纲','state':'QUEUED'}]
    def transport(req):
        body=json.loads(req.content)
        context=json.loads(body['messages'][1]['content'])
        assert context['known_tasks']==known
        result=SemanticResponse(decisions=[TaskDecision(action='CANCEL',reason='explicit cancellation',target_dedup_key='persisted-identity-42')])
        return httpx.Response(200,json={'base_resp':{'status_code':0},'choices':[{'message':{'content':result.model_dump_json()}}]})
    gate=MiniMaxAdapter(api_key='test-only',client=httpx.Client(transport=httpx.MockTransport(transport)),context_provider=lambda _:known)
    assert gate.classify(b).decisions[0].target_dedup_key=='persisted-identity-42'
    invented=SemanticResponse(decisions=[TaskDecision(action='CANCEL',reason='invented',target_dedup_key='unknown-key')])
    with pytest.raises(ValueError):validate_response(invented,b,known)

@pytest.mark.parametrize('code', [1004, 1008, 2049, 2056])
def test_minimax_permanent_business_errors_do_not_retry_or_echo_key(code):
    from pydantic import SecretStr
    calls = []
    key = 'test-private-api-key'
    def transport(req):
        calls.append(req)
        return httpx.Response(200, json={'base_resp': {'status_code': code, 'status_msg': key}})
    gate = MiniMaxAdapter(api_key=SecretStr(key), sleep=lambda _: None,
                          client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert key not in repr(gate.api_key)
    with pytest.raises(MiniMaxError) as error:
        gate.classify(batch('我需要一个 XRD 原理报告大纲'))
    assert len(calls) == 1
    assert key not in str(error.value)
    gate.close()



def test_redundant_output_prefix_is_stripped():
    b=batch('帮我写一个把摄氏度转换成华氏度的 Python 脚本')
    r=MockSemanticAdapter().classify(b)
    task=next(d.task for d in r.decisions if d.action=='APPROVE')
    task.expected_outputs=['output/c2f.py','./output/sub/notes.md','output']
    validate_response(r,b)
    assert task.expected_outputs==['c2f.py','sub/notes.md','output']
