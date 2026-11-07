"""Built-ins and installed said_done.sources entry points.

An entry point exposes a source class accepting (source_id, options, repository)
and an optional Options pydantic model for setup schema discovery.
"""
from importlib.metadata import entry_points
from .interface import TranscriptSource
from .sources import ManualSource, FolderSource, McpSource, ManualOptions, FolderOptions, McpOptions


def _registry():
    kinds={'manual':(ManualSource,ManualOptions),'folder':(FolderSource,FolderOptions),
           'mcp-stdio':(McpSource,McpOptions),'cloud-transcript-sync':(McpSource,McpOptions)}
    for ep in entry_points(group='said_done.sources'):
        if ep.name not in kinds:
            cls=ep.load()
            kinds[ep.name]=(cls,getattr(cls,'Options',ManualOptions))
    return kinds


def list_source_kinds() -> list[dict]:
    return [{'kind':kind,'options_schema':options.model_json_schema()} for kind,(_,options) in _registry().items()]


def build_sources(settings, repository) -> list[TranscriptSource]:
    kinds=_registry()
    sources=[]
    try:
        for config in settings.sources:
            if config.kind not in kinds:
                raise ValueError('unknown transcript source kind')
            cls,model=kinds[config.kind]
            options=dict(config.options)
            if config.kind=='cloud-transcript-sync':
                options['preset']='cloud-transcript-sync'
            options=model.model_validate(options).model_dump()
            sources.append(cls(config.id,options,repository))
        return sources
    except Exception:
        for source in sources:
            source.close()
        raise
