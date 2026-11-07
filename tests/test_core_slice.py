from pathlib import Path

from said_done.config import Settings
from said_done.ingestion.cloud_poll import CloudTranscriptPoll, cloud_status
from said_done.runtime import create_runtime


def test_cloud_cursor_to_local_result_without_external_services(tmp_path):
    start = 1_800_000_000_000
    now = [start]
    calls = []
    rows = [
        {"id": 1, "timeMs": start - 1, "sourceType": "transcript",
         "text": "我需要一个不应重跑的历史报告大纲"},
        {"id": 2, "timeMs": start + 1, "sourceType": "transcript",
         "text": "我需要一个 XRD 原理报告大纲"},
    ]

    class FakeCloud:
        def fetch(self, args):
            calls.append(args)
            records = [r for r in rows if args["fromMs"] <= r["timeMs"] < args["toMs"]
                       and args["sinceId"] < r["id"]]
            return {"results": records, "resultCount": len(records), "truncated": False}

    runtime = create_runtime(Settings(data_dir=tmp_path))
    try:
        repo = runtime.repository
        runtime.poll_source = CloudTranscriptPoll(repo, FakeCloud(), clock_ms=lambda: now[0])
        assert runtime.tick(force=True)["screened"] == 0
        now[0] += 10
        assert runtime.tick(force=True)["accepted"] == 1
        now[0] += 10
        assert runtime.tick(force=True)["screened"] == 0
        assert calls[-1]["sinceId"] == cloud_status(repo)[0]["highwater_id"] == 2
        task, = repo.status()["tasks"]
        result = repo.task_result(task["id"])
        assert result.status == "COMPLETED"
        assert result.artifacts and all(Path(p).is_file() for p in result.artifacts)
        assert repo.audit_summary()["by_outcome"] == {"ACCEPTED": 1}
    finally:
        runtime.close()
