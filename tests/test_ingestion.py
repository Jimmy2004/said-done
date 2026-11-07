import json
from datetime import timedelta
import pytest
from said_done.contracts import utcnow
from said_done.ingestion.manual_input import ManualInput
from said_done.ingestion.json_source import JsonSource
from said_done.ingestion.mock_source import MockSource
from said_done.heartbeat.threshold import should_process

def test_empty_and_manual_stability():
    with pytest.raises(ValueError):ManualInput(' ')
    source=ManualInput('普通短句');assert source.read()==source.read()
    assert not should_process(source.read())
    assert should_process(source.read(),min_content_chars=2)
    assert should_process(source.read(),now=utcnow()+timedelta(seconds=1801))

def test_keyword_is_trigger_only():
    assert should_process(ManualInput('我要整理这个 Excel').read())
    assert not should_process([])

def test_sources_same_schema(tmp_path):
    segments=ManualInput('我需要一个 XRD 原理报告大纲','id').read()
    p=tmp_path/'input.json';p.write_text(json.dumps([s.model_dump(mode='json') for s in segments],ensure_ascii=False),encoding='utf-8')
    assert JsonSource(p).read()==MockSource(tmp_path).read()==segments
