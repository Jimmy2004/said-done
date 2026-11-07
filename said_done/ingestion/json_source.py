import json
from pathlib import Path
from said_done.contracts import TranscriptSegment
class JsonSource:
    def __init__(self,path): self.path=Path(path)
    def read(self):
        value=json.loads(self.path.read_text('utf-8-sig'))
        if isinstance(value,dict) and 'segments' in value: value=value['segments']
        values=value if isinstance(value,list) else [value]
        return [TranscriptSegment.model_validate(v) for v in values]
