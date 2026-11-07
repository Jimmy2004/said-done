"""Read new cloud transcripts with a persistent cutover and resumable scan.

The cursor keeps an incremental watermark (``since_id``) so every tick only
asks for records that arrived since the last complete page, and a bounded,
time-limited reconcile pass that looks back a fixed window to catch records
whose id arrived out of order.  The reconcile never rewinds the normal
watermark and never runs before the initial cutover.
"""
import time


class CloudTranscriptPoll:
    PAGE_SIZE = 300
    PAGES_PER_TICK = 4
    RECONCILE_INTERVAL_MS = 60 * 60 * 1000
    RECONCILE_LOOKBACK_MS = 2 * RECONCILE_INTERVAL_MS

    def __init__(self, repository, client, device='', clock_ms=None, *, source_id=None, transcript_tool='get_transcripts'):
        self.repo = repository
        self.client = client
        self.device = device
        self.cursor_key = device if source_id is None else f"source:{source_id}:{device}"
        self.source_id = source_id
        self.transcript_tool = transcript_tool
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        with self.repo.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS cloud_transcript_cursor (
                device TEXT PRIMARY KEY, cutover_ms INTEGER NOT NULL,
                complete_ms INTEGER NOT NULL, scan_end_ms INTEGER,
                since_id INTEGER NOT NULL DEFAULT 0,
                last_success REAL, last_error TEXT)''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(cloud_transcript_cursor)')}
            # Old workers used since_id only inside a scan. Leave that
            # in-progress scan alone and establish a durable watermark once.
            for name, schema in (
                ('highwater_id', 'INTEGER NOT NULL DEFAULT 0'),
                ('scan_from_ms', 'INTEGER'),
                ('last_reconcile_ms', 'INTEGER'),
            ):
                if name not in columns:
                    conn.execute(f'ALTER TABLE cloud_transcript_cursor ADD COLUMN {name} {schema}')

    def __call__(self):
        # Scheduler holds the single-worker lock, including across network reads.
        now = self.clock_ms()
        with self.repo.db.transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO cloud_transcript_cursor '
                         '(device,cutover_ms,complete_ms) VALUES(?,?,?)',
                         (self.cursor_key, now, now))
            conn.execute('UPDATE cloud_transcript_cursor SET scan_end_ms=?, since_id=highwater_id '
                         'WHERE device=? AND scan_end_ms IS NULL AND complete_ms<=?',
                         (now + 1, self.cursor_key, now))
            state = conn.execute('SELECT * FROM cloud_transcript_cursor WHERE device=?',
                                 (self.cursor_key,)).fetchone()
        if state['scan_end_ms'] is None:
            return True  # Local clock moved backward; don't regress the cursor.
        try:
            if not self._scan(state):
                return False
            if state['scan_from_ms'] is not None:
                return True
            # sinceId cannot see a lower ID that arrives out of order. Once
            # an hour, do a bounded check for those rows, never before cutover.
            with self.repo.db.transaction() as conn:
                conn.execute('''UPDATE cloud_transcript_cursor
                    SET scan_end_ms=?, scan_from_ms=MAX(cutover_ms,?), since_id=0
                    WHERE device=? AND ? - COALESCE(last_reconcile_ms,cutover_ms) >= ?''',
                    (now + 1, now - self.RECONCILE_LOOKBACK_MS, self.cursor_key, now, self.RECONCILE_INTERVAL_MS))
                state = conn.execute('SELECT * FROM cloud_transcript_cursor WHERE device=?',
                                     (self.cursor_key,)).fetchone()
            return state['scan_from_ms'] is None or self._scan(state)
        except Exception as exc:
            # Never persist tool error payloads, which can contain credentials
            # or private text. A failed read blocks this tick, retaining input.
            with self.repo.db.transaction() as conn:
                conn.execute('UPDATE cloud_transcript_cursor SET last_error=? WHERE device=?',
                             (type(exc).__name__, self.cursor_key))
            raise RuntimeError('cloud transcript poll failed; cursor retained') from None

    def _scan(self, state):
        from said_done.ingestion.cloud_mcp_client import normalize_transcript

        since = state['since_id']
        reconcile = state['scan_from_ms'] is not None
        start = state['scan_from_ms'] if reconcile else state['cutover_ms']
        end = state['scan_end_ms']
        for _ in range(self.PAGES_PER_TICK):
            limit = self.PAGE_SIZE
            while True:
                result = self.client.fetch(dict(tool=self.transcript_tool, fromMs=start, toMs=end,
                    sinceId=since, limit=limit, maxChars=2_000_000, device=self.device))
                if result.get('truncated') is False:
                    break
                if result.get('truncated') is not True or limit == 1:
                    raise ValueError('invalid or truncated cloud transcript page')
                limit = max(1, limit // 2)
            records = result['results']
            if (not isinstance(records, list) or len(records) != result['resultCount']
                    or len(records) > limit):
                raise ValueError('invalid cloud page count')
            segments = []
            for record in records:
                rid, at = _wire_int(record['id']), _wire_int(record['timeMs'])
                if (rid <= since or not start <= at < end or record.get('sourceType') != 'transcript'
                        or (record.get('device') or '') != self.device):
                    raise ValueError('cloud response violated transcript scan bounds')
                since = rid
                seg = normalize_transcript(record, self.device)
                if seg is not None:
                    if self.source_id is not None:
                        from said_done.contracts import TranscriptSegment
                        seg=TranscriptSegment.model_validate({**seg.model_dump(),
                            'segment_id':f'{self.source_id}:{seg.segment_id}','source':self.source_id})
                    segments.append(seg)
            # Store first, then checkpoint. Replaying an interrupted page is
            # safe because ingestion deduplicates by stable device/record ID.
            self.repo.ingest(segments)
            complete = len(records) < limit
            with self.repo.db.transaction() as conn:
                if complete:
                    if reconcile:
                        conn.execute('''UPDATE cloud_transcript_cursor SET scan_end_ms=NULL,scan_from_ms=NULL,
                            last_reconcile_ms=?,since_id=highwater_id,last_success=?,last_error=NULL WHERE device=?''',
                            (end - 1, time.time(), self.cursor_key))
                    else:
                        conn.execute('''UPDATE cloud_transcript_cursor SET complete_ms=?,scan_end_ms=NULL,
                            highwater_id=?,since_id=?,last_success=?,last_error=NULL WHERE device=?''',
                            (end, since, since, time.time(), self.cursor_key))
                else:
                    conn.execute('UPDATE cloud_transcript_cursor SET since_id=?,last_error=NULL WHERE device=?',
                                 (since, self.cursor_key))
            if complete:
                return True
        return False


def cloud_status(repository):
    with repository.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_transcript_cursor'").fetchone():
            return []
        return [dict(row) for row in conn.execute('SELECT * FROM cloud_transcript_cursor ORDER BY device')]


def _wire_int(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError('invalid cloud transcript integer')
