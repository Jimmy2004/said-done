from uuid import uuid4
from said_done.contracts import TranscriptSegment,utcnow
class ManualInput:
    def __init__(self,text,segment_id=None,source='manual'):
        now=utcnow()
        self.segment=TranscriptSegment(segment_id=segment_id or str(uuid4()),timestamp_start=now,timestamp_end=now,text=text,source=source)
    def read(self): return [self.segment]
