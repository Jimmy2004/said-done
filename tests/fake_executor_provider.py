#!/usr/bin/env python3
"""Network-free CLI double used for argv, parsing, failure and timeout tests."""
import json
import os
import sys
import time
from pathlib import Path

if '--version' in sys.argv:
    print('fake-provider 1.2.3')
    sys.exit(0)
Path('argv.json').write_text(json.dumps(sys.argv[1:]))
prompt=sys.stdin.read() if '-p' in sys.argv and '--verbose' in sys.argv or '--stdin' in sys.argv else ' '.join(sys.argv[1:])
if os.getenv('FAKE_EXECUTOR_MODE')=='timeout':
    time.sleep(30)
if os.getenv('FAKE_EXECUTOR_MODE')=='overflow':
    print('x'*2_000_000)
    sys.exit(0)
if os.getenv('FAKE_EXECUTOR_MODE')=='error':
    print('PRIVATE_PROMPT',file=sys.stderr)
    sys.exit(2)
if os.getenv('FAKE_EXECUTOR_MODE')=='event-error':
    # opencode-style structured error, as seen with an exhausted free tier
    print(json.dumps({'type':'error','error':{'type':'provider.free-limit','message':'PRIVATE_PROMPT quota'}}))
    sys.exit(1)
Path('output/report.txt').write_text('XRD film characterization report outline. '*10)
if '--format' in sys.argv:
    print(json.dumps({'type':'text','part':{'text':'opencode done'}}))
elif '--verbose' in sys.argv:
    print(json.dumps({'type':'result','result':'claude done','is_error':False}))
else:
    print('command done')
