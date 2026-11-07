"""OS releases this advisory lock on process death; no unsafe expired leases."""
import os
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def single_worker(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise RuntimeError('another worker is active') from exc
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
