import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS host_tasks (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), claimed_at REAL, lease_until REAL);
CREATE TABLE IF NOT EXISTS source_state (
 source_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(source_id,key));
CREATE TABLE IF NOT EXISTS segments (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, received REAL NOT NULL,
 classified INTEGER NOT NULL DEFAULT 0, batch_id TEXT);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, fingerprint TEXT UNIQUE, body TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT, workspace TEXT, error TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, task_id TEXT, state TEXT NOT NULL, detail TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_provenance (
 fingerprint TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(fingerprint,task_id));
CREATE TABLE IF NOT EXISTS screening_audit (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, outcome TEXT NOT NULL, action TEXT, model TEXT NOT NULL,
 reason TEXT, error TEXT, detail TEXT, source_refs TEXT NOT NULL DEFAULT '[]',
 evidence_segment_ids TEXT NOT NULL DEFAULT '[]', task_id TEXT,
 linked_task_ids TEXT NOT NULL DEFAULT '[]', dedup_key TEXT, target_dedup_key TEXT,
 attempt INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS screening_audit_batch ON screening_audit(batch_id);
CREATE INDEX IF NOT EXISTS screening_audit_task ON screening_audit(task_id);
CREATE INDEX IF NOT EXISTS screening_audit_created ON screening_audit(created);
'''

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns={r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
            if 'fingerprint' not in columns:
                conn.execute('ALTER TABLE tasks ADD COLUMN fingerprint TEXT')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS task_fingerprint ON tasks(fingerprint)')
            from said_done.contracts import TaskPacket
            from said_done.broker.deduplication import provenance_fingerprint
            legacy=conn.execute('SELECT id,body FROM tasks WHERE NOT EXISTS (SELECT 1 FROM task_provenance p WHERE p.task_id=tasks.id)').fetchall()
            for row in legacy:
                task=TaskPacket.model_validate_json(row['body'])
                conn.execute('INSERT OR IGNORE INTO task_provenance(fingerprint,task_id) VALUES(?,?)',(provenance_fingerprint(task),row['id']))

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
