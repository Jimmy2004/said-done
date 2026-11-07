"""CLI providers share preflight, copied inputs, process limits and validation."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from string import Formatter
from pydantic import BaseModel, ConfigDict, Field
from said_done.contracts import ExecutionResult
from said_done.executor.mock_executor import MockExecutor
from said_done.executor.preflight import resolve_inputs
from said_done.executor.workspace import prepare_workspace, collect_artifacts
from said_done.executor.prompt import build_execution_prompt
from said_done.executor.artifact_validator import validate_artifacts
from said_done.executor.process_control import communicate_bounded, stop_process


class ProviderRunError(RuntimeError):
    """Failure whose message is built only from fixed text and provider error *types* (never prompt/stderr)."""


def _event_error(event: dict) -> str | None:
    """Provider error type from a JSON event (claude stream-json / opencode json), if any."""
    if event.get('type') == 'result' and event.get('is_error'):
        return str(event.get('subtype') or 'result_error')[:80]
    if event.get('type') == 'error':
        error = event.get('error')
        kind = error.get('type') if isinstance(error, dict) else None
        return str(kind or 'error')[:80]
    return None


class CommandOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    argv: list[str] = Field(default_factory=list)
    prompt_via: str = 'stdin'
    command_confirmed: bool = False
    allow_bash: bool = False
    provider: str | None = None
    agent: str | None = None
    standalone: bool = False


class CommandExecutor(MockExecutor):
    verification='verified'
    def __init__(self, kind, executable=None, model=None, effort=None, options=None, **kwargs):
        super().__init__(**kwargs)
        self.kind=kind
        self.executable=executable or {'claude-code':'claude','opencode':'opencode','pi':'pi'}.get(kind,'')
        self.model,self.effort=model,effort
        self.options=CommandOptions.model_validate(options or {})
        self._processes={}
        if kind in {'pi','dsh','custom-command'}:
            self.verification='experimental-unverified'
        if kind in {'dsh','custom-command'}:
            if not self.options.argv or any(not isinstance(part,str) or not part.strip() for part in self.options.argv):
                raise ValueError('custom-command requires an explicit argv template')
            if kind=='dsh' and not self.options.command_confirmed:
                raise ValueError('dsh command template must be user-confirmed')
            if self.options.prompt_via not in {'stdin','arg','file'}:
                raise ValueError('invalid prompt_via')
            fields={name for part in self.options.argv for _,name,_,_ in Formatter().parse(part) if name is not None}
            if fields-{'workspace','prompt','prompt_file','model','effort'}:
                raise ValueError('unknown command template placeholder')
            if self.options.prompt_via=='arg' and 'prompt' not in fields:
                raise ValueError('arg prompt mode requires {prompt}')
            if self.options.prompt_via=='file' and 'prompt_file' not in fields:
                raise ValueError('file prompt mode requires {prompt_file}')

    @classmethod
    def detect(cls):
        from said_done.executor.registry import executor_info
        return executor_info(getattr(cls,'kind','custom-command'))

    def _build_command(self, workspace: Path, prompt: str, prompt_file: str='') -> list[str]:
        o=self.options
        if self.kind=='claude-code':
            tools='Read,Write,Edit,Glob,Grep'+(',Bash' if o.allow_bash else '')
            command=[self.executable,'-p','--output-format','stream-json','--verbose',
                     '--permission-mode','acceptEdits','--allowedTools',tools]
            if self.model:
                command+=['--model',self.model]
            if self.effort:
                command+=['--effort',self.effort]
            return command
        if self.kind=='opencode':
            command=[self.executable,'run','--format','json','--auto']
            if self.model:
                command+=['--model',self.model]
            if o.standalone:
                command+=['--standalone']
            if o.agent:
                command+=['--agent',o.agent]
            return command+[prompt]
        if self.kind=='pi':
            # Assumption: Pi coding agent print mode. Not installed/verified.
            command=[self.executable,'-p',prompt]
            if o.provider:
                command+=['--provider',o.provider]
            if self.model:
                command+=['--model',self.model]
            return command
        values={'workspace':str(workspace),'prompt':prompt,'prompt_file':prompt_file,
                'model':self.model or '', 'effort':self.effort or ''}
        return [part.format_map(values) for part in o.argv]

    def _summary(self, stdout):
        if self.kind in {'pi','dsh','custom-command'}:
            return stdout.strip()[:4000]
        messages=[]
        for line in stdout.splitlines():
            try:
                event=json.loads(line)
            except ValueError:
                continue
            if not isinstance(event,dict):
                continue
            error=_event_error(event)
            if error:
                hint = "; set executor.model to a connected provider's model" if self.kind == 'opencode' and error == 'provider.free-limit' else ''
                raise ProviderRunError(f'{self.kind} reported error: {error}{hint}')
            text=event.get('result')
            if self.kind=='opencode':
                part=event.get('part',{})
                text=part.get('text') if isinstance(part,dict) else None
            if isinstance(text,str):
                messages.append(text)
        return '\n'.join(messages)[-4000:]

    def execute(self, task, workspace):
        workspace=Path(workspace)
        result=None
        process=None
        prompt_path=None
        try:
            if self.preflight(task,workspace).status!='READY':
                raise RuntimeError('preflight blocked')
            inputs,errors=resolve_inputs(task,workspace,self.preflight_config)
            if errors:
                raise RuntimeError('input resolution failed')
            plan=prepare_workspace(task,workspace,inputs,self.execution_config)
            prompt=build_execution_prompt(task,plan)
            if self.options.prompt_via=='file':
                fd,prompt_path=tempfile.mkstemp(prefix='.prompt-',dir=workspace)
                with os.fdopen(fd,'w') as stream:
                    stream.write(prompt)
            command=self._build_command(workspace,prompt,prompt_path or '')
            process=subprocess.Popen(command,cwd=workspace,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',start_new_session=os.name!='nt')
            with self._lock:
                self._processes[task.task_id]=process
                self._status[task.task_id]='RUNNING'
            via_stdin=self.kind=='claude-code' or self.kind in {'dsh','custom-command'} and self.options.prompt_via=='stdin'
            stdout,_=communicate_bounded(process,input=prompt if via_stdin else None,
                                          timeout=self.execution_config.timeout_seconds)
            # Parse first: structured error events explain a non-zero exit.
            summary=self._summary(stdout)
            if process.returncode:
                raise ProviderRunError(f'{self.kind} exited with code {process.returncode}')
            artifacts=[str(p) for p in collect_artifacts(workspace,self.execution_config)]
            result=ExecutionResult(task_id=task.task_id,status='COMPLETED',summary=summary or 'executor finished',artifacts=artifacts)
            if validate_artifacts(task,result,workspace,self.validator_config):
                result=ExecutionResult(task_id=task.task_id,status='FAILED',summary='artifact verification failed',error='invalid artifacts')
        except Exception as exc:
            error=str(exc) if isinstance(exc,ProviderRunError) else type(exc).__name__
            result=ExecutionResult(task_id=task.task_id,status='FAILED',summary='executor failed',error=error)
        finally:
            stop_process(process)
            if prompt_path:
                Path(prompt_path).unlink(missing_ok=True)
            with self._lock:
                self._processes.pop(task.task_id,None)
        with self._lock:
            self._status[task.task_id]=result.status
            self._results[task.task_id]=result
        return result

    def cancel(self, task_id):
        with self._lock:
            process=self._processes.get(task_id)
        stop_process(process)
        super().cancel(task_id)

    def close(self):
        for process in list(self._processes.values()):
            stop_process(process)


class HostExecutor(MockExecutor):
    kind='host'
    verification='verified'
    host_mode=True
    def execute(self, task, workspace):
        raise RuntimeError('host tasks must be claimed through host_queue')
    @classmethod
    def detect(cls):
        from said_done.executor.registry import executor_info
        return executor_info(cls.kind)
