"""MOCK_ONLY: conservative example classifier, never a real MiniMax substitute."""
import hashlib
import re
from pathlib import Path
from said_done.contracts import *
from said_done.config import GateConfig

def _key(goal):
    value=re.sub(r'[\s，。！？、：:,.!?]','',goal).casefold()
    return hashlib.sha256(value.encode()).hexdigest()

class MockSemanticAdapter:
    model = "mock"

    def __init__(self, gate: GateConfig | None = None):
        self.gate = gate or GateConfig()

    def close(self) -> None:
        pass

    def classify(self,batch):
        decisions=[];seen=set()
        # Merge only a neighbouring explicit missing-topic continuation.
        groups=[]
        for s in sorted(batch.segments,key=lambda s:s.timestamp_start):
            if re.match(r'^(主题|内容|关于|题目)',s.text) and groups and any(x in groups[-1][0] for x in ['大纲','报告','模板']):
                prev,ss=groups[-1];groups[-1]=(prev+'；'+s.text,ss+[s])
            else:
                groups.append((s.text,[s]))
        for text,segments in groups:
            if re.match(r'^[“"「『]',text) or any(x in text for x in ['假设','如果','别人说','他说','她说','引用','朋友说','我去查','查一下','查询','天气','怎么','是什么']):
                decisions.append(TaskDecision(action='IGNORE',reason='mock: hypothetical, quoted or query'));continue
            control=any(x in text for x in self.gate.cancel_keywords + self.gate.completion_keywords)
            if control:
                # Only resolve a previous explicit task by a distinctive subject.
                candidates=[]
                for prior in decisions:
                    if prior.task:
                        words=re.findall(r'[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}',prior.task.goal)
                        if any(w.casefold() in text.casefold() for w in words): candidates.append(prior.task)
                if len(candidates)==1:
                    decisions.append(TaskDecision(action='CANCEL' if any(x in text for x in self.gate.cancel_keywords) else 'ALREADY_COMPLETED',reason='explicit control signal',target_dedup_key=candidates[0].dedup_key))
                else:
                    decisions.append(TaskDecision(action='IGNORE',reason='mock: no unique cancellation target'))
                continue
            if not any(x in text for x in ['我要','我需要','帮我','请帮我','我想要','我想写']):
                decisions.append(TaskDecision(action='IGNORE',reason='mock: no explicit personal intent'));continue
            task=None
            if re.search(r'转[成为]|转换',text) and 'CSV' in text.upper():
                m=re.search(r'([A-Za-z]:[\\/][^\n\r"<>|?*]*?\.(?:xlsx|csv))|([\w\u4e00-\u9fff.-]+\.xlsx)',text,re.I)
                if m:
                    raw=m.group(0)
                    if not re.match(r'^[A-Za-z]:',raw) and '桌面' in text:
                        raw=str(Path.home()/'Desktop'/raw)
                    if raw:
                        stem=raw.replace('\\','/').rsplit('/',1)[-1].rsplit('.',1)[0]
                        goal=f'将 {raw} 转换为 CSV'
                        task=TaskPacket(task_type='CONVERT_FILE',goal=goal,input_paths=[raw],expected_outputs=[stem+'.csv'],dedup_key=_key(goal),
                          evidence_segment_ids=[s.segment_id for s in segments],original_evidence=[s.text for s in segments],user_facts=[text],forbidden_assumptions=['不得补造数据或替代缺失输入'])
            elif any(x in text for x in ['大纲','报告草稿','计划书','文档模板','Excel模板','Excel 模板','演示文稿框架','脚本','代码']):
                goal=re.sub(r'^(我需要|我要|帮我|请帮我|我想要|我想写)(一个|一份|个)?','',text).strip(' ，。')
                generic=re.sub(r'(一个|一份|报告|草稿|大纲|文档|模板|我需要|我要|；|主题是|内容是|的|请|帮我)','',goal).strip(' ，。')
                if len(generic)>=2:
                    code=any(w in text for w in ['脚本','代码'])
                    ext=self.gate.default_code_format if code else self.gate.default_document_format
                    key=_key(goal)
                    task=TaskPacket(task_type='WRITE_CODE' if code else 'CREATE_DOCUMENT',goal=goal,
                         evidence_segment_ids=[s.segment_id for s in segments],original_evidence=[s.text for s in segments],
                         user_facts=[text],defaults={'format':ext},expected_outputs=[('script' if code else 'outline')+'.'+ext],
                         forbidden_assumptions=['不得虚构业务数据、路径或未表达的要求'],dedup_key=key)
            if task and task.task_type not in self.gate.allowed_task_types:
                task=None
            if task and task.dedup_key not in seen:
                seen.add(task.dedup_key);decisions.append(TaskDecision(action='APPROVE',reason='mock: explicit task',task=task))
            elif task:
                decisions.append(TaskDecision(action='IGNORE',reason='mock: duplicate task'))
            else:
                decisions.append(TaskDecision(action='IGNORE',reason='mock: insufficient target/context'))
        return SemanticResponse(decisions=decisions)
