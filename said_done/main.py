"""Local entry point. Importing this module never starts external services."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from said_done.config import load_settings, resolve_config_path
from said_done.storage.database import Database
from said_done.storage.repositories import Repository

def build_runtime(settings):
    # Concrete constructors are assembled in one place; source adapters never
    # need to know which semantic or execution implementation is used.
    from said_done.runtime import create_runtime
    return create_runtime(settings)

def main(argv=None):
    parser=argparse.ArgumentParser(description='ASR task framework V0.2 (offline mock by default)')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--data-dir',type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    manual=sub.add_parser('submit',help='persist a single manual transcript')
    manual.add_argument('text');manual.add_argument('--segment-id');manual.add_argument('--source',default='manual')
    batch=sub.add_parser('import-json',help='persist a JSON segment or segment array')
    batch.add_argument('path',type=Path)
    mock=sub.add_parser('import-mock',help='persist JSON samples through the same ingestion boundary')
    mock.add_argument('path',type=Path)
    sub.add_parser('process-now',help='run pending work through the actual heartbeat pipeline now')
    sub.add_parser('run',help='run the local background heartbeat until interrupted')
    sub.add_parser('sources',help='list transcript source kinds and option schemas')
    sub.add_parser('executors',help='detect executor providers and option schemas')
    sub.add_parser('status',help='show persistent classification and task states')
    result_cmd=sub.add_parser('task-result',help='read the stored local execution result')
    result_cmd.add_argument('task_id')
    sub.add_parser('source-status',help='show cloud transcript checkpoints without transcript contents')
    audit=sub.add_parser('screening-audit',help='inspect MiniMax admission decisions')
    audit.add_argument('--outcome',choices=['ACCEPTED','REJECTED','ERROR'])
    audit.add_argument('--batch-id')
    audit.add_argument('--task-id')
    audit.add_argument('--limit',type=int,default=30,choices=range(1,501),metavar='1..500')
    audit.add_argument('--summary',action='store_true')
    sub.add_parser('retry-classification',help='explicitly requeue exhausted semantic batches')
    args=parser.parse_args(argv)
    settings=load_settings(resolve_config_path(args.config))
    if args.data_dir:
        settings.data_dir=args.data_dir.resolve()
    settings.data_dir.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(handlers=[RotatingFileHandler(settings.data_dir/'worker.log',maxBytes=5_000_000,backupCount=3,encoding='utf-8')],level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    repo=Repository(Database(settings.data_dir/'core.db'))
    if args.command in {'sources','executors'}:
        from said_done.ingestion.registry import list_source_kinds
        from said_done.executor.registry import list_executors
        print(json.dumps(list_source_kinds() if args.command=='sources' else list_executors(),ensure_ascii=False,indent=2));return
    if args.command=='source-status':
        from said_done.ingestion.cloud_poll import cloud_status
        print(json.dumps(cloud_status(repo),ensure_ascii=False,indent=2));return
    if args.command=='screening-audit':
        result=repo.audit_summary() if args.summary else repo.audit_records(limit=args.limit,
            outcome=args.outcome,batch_id=args.batch_id,task_id=args.task_id)
        print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.command=='submit':
        from said_done.ingestion.manual_input import ManualInput
        segments=ManualInput(args.text,args.segment_id,args.source).read()
        print(json.dumps({'inserted':repo.ingest(segments)},ensure_ascii=False));return
    if args.command in {'import-json','import-mock'}:
        from said_done.ingestion.json_source import JsonSource
        from said_done.ingestion.mock_source import MockSource
        segments=(JsonSource(args.path) if args.command=='import-json' else MockSource(args.path)).read()
        print(json.dumps({'inserted':repo.ingest(segments)},ensure_ascii=False));return
    if args.command=='status':
        print(json.dumps(repo.status(),ensure_ascii=False,indent=2));return
    if args.command=='task-result':
        result=repo.task_result(args.task_id)
        print(result.model_dump_json(indent=2) if result else 'null');return
    if args.command=='retry-classification':
        print(json.dumps({'requeued':repo.requeue_classification()}));return
    runtime=build_runtime(settings)
    try:
        if args.command=='process-now':
            runtime.tick(force=True)
            print(json.dumps(repo.status(),ensure_ascii=False,indent=2))
        elif args.command=='run':
            import signal
            import threading
            stop_event=threading.Event()
            previous={sig:signal.signal(sig,lambda *_:stop_event.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
            try:
                runtime.run(stop_event)
            finally:
                for sig,old in previous.items():
                    signal.signal(sig,old)
    finally:
        runtime.close()

if __name__=='__main__':
    main()
