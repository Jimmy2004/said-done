"""Bounded subprocess I/O and cleanup of adapter-owned process groups."""
from __future__ import annotations
import os
import signal
import subprocess
import threading
from pathlib import Path

MAX_OUTPUT=1_048_576


def stop_process(process):
    if process is None:
        return
    if os.name=='nt':
        if process.poll() is not None:
            return
        taskkill=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/taskkill.exe'
        subprocess.run([str(taskkill),'/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=10)
    else:
        try:
            # Only kill a group if this process started its own session.
            if os.getpgid(process.pid)==process.pid:
                os.killpg(process.pid,signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            # The group can outlive its leader when grandchildren hold pipes.
            try:
                os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def communicate_bounded(process, input=None, timeout=900, limit=MAX_OUTPUT):
    """Drain both pipes concurrently; fail closed on overflow or timeout."""
    output=[[],[]]
    sizes=[0,0]
    overflow=threading.Event()
    def drain(stream,index):
        try:
            while True:
                data=stream.read(4096)
                if not data:
                    break
                sizes[index]+=len(data)
                if sizes[index]>limit:
                    overflow.set()
                    stop_process(process)
                    break
                output[index].append(data)
        except (OSError,ValueError):
            pass
    readers=[threading.Thread(target=drain,args=(pipe,i),daemon=True)
             for i,pipe in enumerate((process.stdout,process.stderr))]
    for thread in readers:
        thread.start()
    def write():
        if process.stdin:
            try:
                if input is not None:
                    process.stdin.write(input)
                    process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError,OSError,ValueError):
                pass
    writer=threading.Thread(target=write,daemon=True)
    writer.start()
    import time
    deadline=time.monotonic()+timeout
    try:
        process.wait(timeout=timeout)
        for thread in readers:
            thread.join(max(0,deadline-time.monotonic()))
        if any(thread.is_alive() for thread in readers):
            raise subprocess.TimeoutExpired('executor',timeout)
        if overflow.is_set():
            raise RuntimeError('process output limit exceeded')
        return tuple(''.join(chunks) for chunks in output)
    finally:
        stop_process(process)
        for thread in readers:
            thread.join(1)
