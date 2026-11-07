"""Classifier construction shared by the CLI, scheduler and MCP tools."""
from __future__ import annotations

from said_done.config import Settings
from said_done.contracts import TranscriptBatch, TranscriptSegment, utcnow
from said_done.semantic.gate import SemanticGate
from said_done.semantic.mock_adapter import MockSemanticAdapter


MINIMAX_DEFAULT_MODEL = 'MiniMax-M3.1-Flash'


def resolve_screening_model(config) -> str:
    """The screening model of a classifier configuration.

    A subscription falls back to the cheapest model its CLI serves, because
    screening is a light classification and the cheap tier saves quota
    (docs/DESIGN.md §11).  An API provider has no built-in default, so an
    unset model is a configuration error the setup agent resolves through
    ``list_models`` instead of sending a placeholder.
    """
    from said_done.providers.factory import default_model_for_kind

    kind = getattr(config, 'kind', 'mock')
    configured = (getattr(config, 'model', None) or '').strip()
    default = default_model_for_kind(kind)
    if default is not None:
        # 'default' stays a valid alias for the subscription CLIs: it means
        # "whatever the CLI itself uses by default".
        return configured or default
    if kind == 'minimax':
        # MiniMax keeps its own default, and the placeholder 'default' it
        # accepted before is still accepted.
        return configured or MINIMAX_DEFAULT_MODEL
    if configured and configured.casefold() != 'default':
        return configured
    raise ValueError(
        f'no screening model configured for classifier kind {kind!r}: '
        'call list_models and pick/configure a model'
    )


def build_classifier(settings: Settings, repository):
    """Build a classifier exposing classify, model and close."""
    if settings.classifier.kind == 'mock':
        return MockSemanticAdapter(settings.screening.gate)
    config = settings.classifier
    # The model is resolved first: an API provider without a model is a
    # configuration error and must be reported before a provider is built and
    # before any credential is looked up.
    model = resolve_screening_model(config)
    from said_done.providers import build_model_provider
    return SemanticGate(
        build_model_provider(settings), model=model, effort=config.effort,
        gate=settings.screening.gate, max_attempts=config.max_attempts,
        context_provider=repository.semantic_context,
    )


class _SyntheticRepository:
    """An empty in-memory trusted context; a probe never persists user data."""
    def semantic_context(self, batch=None) -> list[dict]:
        return []


def test_classifier(settings: Settings) -> dict:
    """Run one synthetic classification and return a transcript-free summary."""
    classifier = None
    diagnostics = {}
    try:
        classifier = build_classifier(settings, _SyntheticRepository())
        provider = getattr(classifier, 'provider', None)
        if callable(getattr(provider, 'smoke_test', None)):
            diagnostics['program_smoke'] = provider.smoke_test()
            if not diagnostics['program_smoke']['ok']:
                return {'ok': False, **diagnostics}
        now = utcnow()
        batch = TranscriptBatch(batch_id='synthetic-probe', segments=[TranscriptSegment(
            segment_id='synthetic-probe-1', timestamp_start=now, timestamp_end=now,
            text='我需要一个 XRD 原理报告大纲', source='synthetic',
        )])
        response = classifier.classify(batch)
        from .validator import validate_response
        validate_response(response, batch, [], settings.screening.gate)
        return {'ok': True, **diagnostics, 'model': classifier.model,
                'actions': [d.action for d in response.decisions],
                'decision_count': len(response.decisions)}
    except Exception as exc:
        result = {'ok': False, **diagnostics, 'error': type(exc).__name__}
        if type(exc).__name__ == 'ClassificationError':
            # Message is built only from attempt counts and error categories.
            result['detail'] = str(exc)[:200]
        return result
    finally:
        if classifier is not None:
            try:
                classifier.close()
            except Exception:
                pass


# This public diagnostic is not a pytest test when imported by test modules.
test_classifier.__test__ = False
