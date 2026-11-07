"""Disposable local decision programs and their agent prompts."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

from said_done.contracts import SemanticResponse

_DECIDE = r'''
import json
import sys
from pathlib import Path
sys.path.insert(0, REPO_ROOT)
from pydantic import ValidationError
from said_done.contracts import SemanticResponse, TranscriptBatch
from said_done.config import GateConfig
from said_done.semantic.validator import validate_response

root = Path(__file__).resolve().parent
args = sys.argv[1:]
if len(args) != 2 or args[0] not in ('N', 'submit'):
    print('Usage: decide.py N "<reason>" | decide.py submit <draft.json>')
    sys.exit(64)
count_path = root / 'attempts.txt'
try:
    failures = int(count_path.read_text(encoding='utf-8'))
except (OSError, ValueError):
    failures = 0
if failures >= 5:
    print('ATTEMPT LIMIT REACHED')
    sys.exit(2)
if args[0] == 'N':
    reason = args[1][:500]
    payload = {'decisions': [{'action': 'IGNORE', 'reason': reason}] if reason else []}
    (root / 'final.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    print('ACCEPTED')
    sys.exit(0)
else:
    context = json.loads((root / 'context.json').read_text(encoding='utf-8'))
    try:
        response = SemanticResponse.model_validate(json.loads(Path(args[1]).read_text(encoding='utf-8')))
        response = validate_response(response, TranscriptBatch.model_validate(context['batch']),
                                     context.get('known_tasks'), GateConfig.model_validate(context['gate']))
    except Exception as exc:
        count_path.write_text(str(failures + 1), encoding='utf-8')
        # Never print inputs, exception reprs, or Pydantic context (which can include inputs).
        lines = [type(exc).__name__]
        if isinstance(exc, ValidationError):
            for error in exc.errors(include_input=False, include_context=False, include_url=False)[:18]:
                lines.append(str(error['loc']) + ': ' + error['msg'])
        elif type(exc) is ValueError:
            lines.append(str(exc))
        else:
            lines.append('Check draft.json syntax and fields against schema.json.')
        text = '\n'.join(lines)
        for segment in context.get('batch', {}).get('segments', []):
            transcript = segment.get('text', '')
            if transcript:
                text = text.replace(transcript, '[redacted]')
                text = text.replace(repr(transcript)[1:-1], '[redacted]')
        print('\n'.join(line[:400] for line in text.splitlines()[:20]))
        sys.exit(1)
(root / 'final.json').write_text(response.model_dump_json(), encoding='utf-8')
print('ACCEPTED')
'''

_YN = '''import sys
from pathlib import Path
if len(sys.argv) != 2 or sys.argv[1] not in ('Y', 'N'):
    sys.exit(64)
Path(__file__).resolve().with_name('answer.txt').write_text(sys.argv[1], encoding='utf-8')
print('OK')
'''


def create_decision_program(workdir: Path, validation_context: dict) -> None:
    """Write local helpers using the server interpreter and production validators."""
    workdir.mkdir(parents=True, exist_ok=True)
    repo = str(Path(__file__).resolve().parents[2])
    (workdir / 'decide.py').write_text('REPO_ROOT = ' + repr(repo) + '\n' + _DECIDE, encoding='utf-8')
    (workdir / 'yn.py').write_text(_YN, encoding='utf-8')
    (workdir / 'context.json').write_text(json.dumps(validation_context, ensure_ascii=False), encoding='utf-8')
    (workdir / 'schema.json').write_text(json.dumps(SemanticResponse.model_json_schema(), ensure_ascii=False), encoding='utf-8')


def read_final_answer(workdir: Path) -> str:
    """Accept only a bounded regular file; stdout is never a decision."""
    from .codex_subscription import read_bounded_answer
    return read_bounded_answer(workdir / 'final.json')


def _command(workdir: Path, script: str) -> str:
    return shlex.join([sys.executable, str(workdir.resolve() / script)])


def build_decision_prompt(messages: list[dict], workdir: Path) -> str:
    from .codex_subscription import render_prompt
    command = _command(workdir, 'decide.py')
    return render_prompt(messages) + f'''

Do not answer in prose. Your answer is recorded ONLY by running the decision program.
If no task should be approved/cancelled/completed, run: `{command} N "<short reason>"`.
Otherwise write {workdir.resolve() / 'draft.json'} following {workdir.resolve() / 'schema.json'}
and run `{command} submit {shlex.quote(str(workdir.resolve() / 'draft.json'))}`;
if it prints errors, fix draft.json and run it again. Stop as soon as it prints ACCEPTED.
Never modify decide.py, context.json or schema.json.
'''


def build_smoke_prompt(workdir: Path) -> str:
    command = _command(workdir, 'yn.py')
    return f'Is 2 + 2 equal to 4? Answer only by running exactly one of: `{command} Y` or `{command} N`. Do nothing else.'
