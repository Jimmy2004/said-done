import json
from datetime import datetime, timezone
from said_done.config import Settings
from said_done.contracts import TranscriptSegment
from said_done.storage.database import Database
from said_done.storage.repositories import Repository
from said_done.ingestion.interface import SourceContext, PollResult
from said_done.ingestion.registry import build_sources,list_source_kinds
from said_done.runtime import create_runtime


def test_folder_restart_dedup_and_kv_isolation(tmp_path):
    inbox=tmp_path/'inbox'
    inbox.mkdir()
    path=inbox/'one.txt'
    path.write_text('XRD 原理与薄膜表征')
    settings=Settings(sources=[{'id':'notes','kind':'folder','options':{'path':str(inbox)}}])
    repo=Repository(Database(tmp_path/'core.db'))
    source,=build_sources(settings,repo)
    ctx=SourceContext(repo,source.id)
    result=source.poll(ctx)
    assert result.complete and len(result.segments)==1
    assert repo.pending()[0].segment_id.startswith('notes:')
    assert not path.exists() and (inbox/'processed/one.txt').exists()
    # Same path+content remains deduplicated even after moving it back/restarting.
    (inbox/'processed/one.txt').replace(path)
    repo=Repository(Database(tmp_path/'core.db'))
    source,=build_sources(settings,repo)
    assert source.poll(SourceContext(repo,source.id)).segments==[]
    assert len(repo.pending())==1
    a,b=SourceContext(repo,'a'),SourceContext(repo,'b')
    a.set('cursor',{'id':42})
    assert a.get('cursor')=={'id':42} and b.get('cursor') is None


def test_json_and_namespaces(tmp_path):
    moment=datetime.now(timezone.utc)
    segment=TranscriptSegment(segment_id='same',source='original',text='XRD',timestamp_start=moment,timestamp_end=moment)
    sources=[]
    for name in ['a','b']:
        folder=tmp_path/name
        folder.mkdir()
        (folder/'segment.json').write_text(json.dumps([segment.model_dump(mode='json')]))
        sources.append({'id':name,'kind':'folder','options':{'path':str(folder)}})
    repo=Repository(Database(tmp_path/'core.db'))
    for source in build_sources(Settings(sources=sources),repo):
        source.poll(SourceContext(repo,source.id))
    assert {s.segment_id for s in repo.pending()}=={'a:same','b:same'}


def test_entrypoint_schema_and_loading(tmp_path,monkeypatch):
    import said_done.ingestion.registry as registry
    from said_done.ingestion.sources import ManualSource,ManualOptions
    class Plugin(ManualSource):
        Options=ManualOptions
    class EP:
        name='third-party'
        def load(self):
            return Plugin
    monkeypatch.setattr(registry,'entry_points',lambda **kw:[EP()] if kw['group']=='said_done.sources' else [])
    assert any(s['kind']=='third-party' and s['options_schema']['type']=='object' for s in list_source_kinds())
    repo=Repository(Database(tmp_path/'core.db'))
    assert build_sources(Settings(sources=[{'id':'third','kind':'third-party'}]),repo)[0].id=='third'


def test_any_incomplete_blocks_and_all_polled(tmp_path):
    rt=create_runtime(Settings(data_dir=tmp_path))
    calls=[]
    class Source:
        id='one'
        def __init__(self,complete):
            self.complete=complete
        def poll(self,ctx):
            calls.append(self.complete)
            return PollResult(complete=self.complete)
    rt.sources=[Source(False),Source(True)]
    rt.repository.ingest([TranscriptSegment(segment_id='1',source='manual',text='我需要一个 XRD 原理报告大纲',
        timestamp_start=datetime.now(timezone.utc),timestamp_end=datetime.now(timezone.utc))])
    assert not rt.tick(force=True)['polled']
    assert calls==[False,True] and not rt.repository.retry_batches()


def test_mcp_namespaces_and_legacy_ids(tmp_path,monkeypatch):
    from said_done.ingestion.sources import McpSource
    from said_done.ingestion.cloud_poll import CloudTranscriptPoll
    from said_done.ingestion.cloud_mcp_client import CloudTranscriptClient
    calls=[]
    now=[1_800_000_000_000]
    monkeypatch.setattr('said_done.ingestion.cloud_poll.time.time_ns',lambda:now[0]*1_000_000)
    def fetch(self,args):
        calls.append(args)
        rows=[{'id':1,'timeMs':now[0]-1,'device':'phone','sourceType':'transcript','text':'XRD'}] if args['sinceId']==0 and args['fromMs']<now[0]-1 else []
        return {'results':rows,'resultCount':len(rows),'truncated':False}
    monkeypatch.setattr(CloudTranscriptClient,'fetch',fetch)
    repo=Repository(Database(tmp_path/'core.db'))
    sources=[McpSource('one',{'command':['fake'],'device':'phone','transcript_tool':'custom'},repo),
             McpSource('two',{'command':['fake'],'device':'phone','preset':'cloud-transcript-sync'},repo)]
    for source in sources:
        source.poll(SourceContext(repo,source.id))
    now[0]+=10
    for source in sources:
        source.poll(SourceContext(repo,source.id))
    assert {s.segment_id for s in repo.pending()}=={'one:cloud-transcript-sync:phone:1','cloud-transcript-sync:phone:1'}
    assert any(c['tool']=='custom' for c in calls)


def test_source_failure_redacts_content(tmp_path):
    import pytest
    rt=create_runtime(Settings(data_dir=tmp_path))
    class Source:
        id='bad'
        def poll(self,ctx):
            raise ValueError('PRIVATE_TRANSCRIPT')
    rt.sources=[Source()]
    with pytest.raises(RuntimeError) as caught:
        rt.tick(force=True)
    assert 'PRIVATE_TRANSCRIPT' not in str(caught.value)


def test_scheduler_passes_nested_keywords_and_gate(tmp_path,monkeypatch):
    import said_done.heartbeat.scheduler as scheduler
    import said_done.semantic.validator as validator
    settings=Settings(data_dir=tmp_path,screening={'screening_only':True,
        'batch':{'trigger_keywords':['special']},'gate':{'default_document_format':'txt'}})
    rt=create_runtime(settings)
    now=datetime.now(timezone.utc)
    rt.repository.ingest([TranscriptSegment(segment_id='1',source='manual',text='special topic',
                                            timestamp_start=now,timestamp_end=now)])
    seen={}
    def threshold(*args,keywords):
        seen['keywords']=keywords
        return True
    original=validator.validate_response
    def validate(response,batch,known_tasks=None,gate=None):
        seen['gate']=gate
        return original(response,batch,known_tasks=known_tasks,gate=gate)
    monkeypatch.setattr(scheduler,'should_process',threshold)
    monkeypatch.setattr(validator,'validate_response',validate)
    rt.tick()
    assert seen['keywords']==['special']
    assert seen['gate'] is settings.screening.gate
    rt.close()
