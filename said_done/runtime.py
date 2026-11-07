"""Composition root: every adapter shares the same admission and execution guards."""
from typing import TYPE_CHECKING
from said_done.config import Settings
if TYPE_CHECKING:
    from said_done.heartbeat.scheduler import Scheduler
from said_done.storage.database import Database
from said_done.storage.repositories import Repository
from said_done.broker.task_manager import TaskManager


def create_runtime(settings: Settings) -> "Scheduler":
    from said_done.heartbeat.scheduler import Scheduler
    from said_done.semantic.factory import build_classifier
    from said_done.executor.registry import build_executor
    from said_done.executor.artifact_validator import validate_artifacts
    from said_done.ingestion.registry import build_sources
    # Cloud data must never be automatically admitted by a deterministic mock.
    cloud=any(s.kind=='cloud-transcript-sync' or s.options.get('preset')=='cloud-transcript-sync' for s in settings.sources)
    if cloud and settings.classifier.kind=='mock':
        raise ValueError('Cloud transcripts require a real classifier')
    if cloud and not settings.screening.screening_only and settings.executor.kind=='mock':
        raise ValueError('Cloud execution requires a real executor; use screening_only first')
    repository=Repository(Database(settings.data_dir/'core.db'))
    semantic=build_classifier(settings,repository)
    executor=build_executor(settings)
    broker=TaskManager(repository.db,executor,settings.data_dir/'tasks',validate_artifacts)
    sources=build_sources(settings,repository)
    # Retain the original server-aligned precheck algorithm for a single cloud
    # source; mixed sources use their own next_deadline values in the runner.
    schedule=getattr(sources[0],'schedule',None) if len(sources)==1 else None
    return Scheduler(settings,repository,semantic,broker,sources=sources,
                     screening_only=settings.screening.screening_only,poll_schedule=schedule)
