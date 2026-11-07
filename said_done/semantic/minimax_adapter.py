"""MiniMax transport; validation, not model prose, controls approval."""
import json
import os
import time
import httpx
from pydantic import SecretStr
from said_done.contracts import SemanticResponse
from .prompt import build_messages
from .validator import validate_response

class MiniMaxError(RuntimeError): pass

class MiniMaxAdapter:
    def __init__(self,api_key=None,model='MiniMax-M3.1-Flash',base_url='https://api.minimaxi.com',
                 client=None,max_attempts=3,sleep=time.sleep,timeout=60,use_response_format=False,context_provider=None):
        key=api_key or os.getenv('MINIMAX_API_KEY')
        if not key: raise ValueError('MINIMAX_API_KEY required')
        self.api_key=key if isinstance(key,SecretStr) else SecretStr(key)
        if max_attempts<1 or max_attempts>5: raise ValueError('max_attempts must be 1..5')
        self.model,self.base_url,self.max_attempts,self.sleep=model,base_url.rstrip('/'),max_attempts,sleep
        self.client=client or httpx.Client(timeout=timeout)
        self.timeout=timeout;self.use_response_format=use_response_format
        self.context_provider=context_provider
        self.reasoning_effort='low' if model=='MiniMax-M3.1-Flash' else None

    def classify(self,batch):
        known_tasks=self.context_provider(batch) if self.context_provider else None
        messages=build_messages(batch,known_tasks)
        last='request failed'
        for attempt in range(self.max_attempts):
            try:
                body={'model':self.model,'messages':messages,'stream':False,'max_completion_tokens':8192}
                endpoint='/v1/text/chatcompletion_v2'
                if self.model in {'MiniMax-M3','MiniMax-M3.1-Flash'}:
                    endpoint='/v1/chat/completions'
                    body['thinking']={'type':'adaptive'}
                if self.reasoning_effort is not None:
                    body['reasoning_effort']=self.reasoning_effort
                if self.use_response_format:
                    body['response_format']={'type':'json_schema','json_schema':{'name':'task_gate','schema':SemanticResponse.model_json_schema()}}
                r=self.client.post(self.base_url+endpoint,json=body,
                    headers={'Authorization':'Bearer '+self.api_key.get_secret_value()},timeout=self.timeout)
                if r.status_code in {401,403}:
                    raise MiniMaxError('MiniMax authentication rejected')
                r.raise_for_status()
                data=r.json()
                code=data.get('base_resp',{}).get('status_code',0)
                if code:
                    if code in {1004,1008,2049,2056}: raise MiniMaxError(f'MiniMax credential/balance/quota error {code}')
                    last=f'business_status_{code}'
                    if attempt+1<self.max_attempts:
                        self.sleep(min(2**attempt,8));continue
                    break
                content=data['choices'][0]['message']['content']
                if not isinstance(content,str) or not content.strip(): raise ValueError('empty model content')
                content=content.strip()
                if content.startswith('```'):
                    content=content.split('\n',1)[-1].rsplit('```',1)[0].strip()
                response=SemanticResponse.model_validate_json(content)
                return validate_response(response,batch,known_tasks)
            except MiniMaxError:
                raise
            except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError) as exc:
                # Do not persist raw provider output or authorization values.
                last=type(exc).__name__
                if attempt+1<self.max_attempts:
                    messages=build_messages(batch,known_tasks)+[{'role':'user','content':'上次输出无效，请严格按Schema返回JSON，不得添加解释。'}]
                    self.sleep(min(2**attempt,8))
        raise MiniMaxError(f'MiniMax classification failed after {self.max_attempts} attempts ({last})')

    def close(self) -> None:
        self.client.close()
