"""Plain-language screening policy for setup agents."""
from said_done.config import ScreeningConfig, Settings


def explain_screening(settings: Settings) -> dict:
    batch, gate = settings.screening.batch, settings.screening.gate
    return {
        'values': settings.screening.model_dump(mode='json'),
        'defaults': ScreeningConfig().model_dump(mode='json'),
        'explanation': {
            'when': f'满足任一条件就判断：内容攒够 {batch.min_content_chars} 字；出现触发词；最早一条等待超过 {batch.max_batch_wait_seconds} 秒。',
            'trigger_keywords': list(batch.trigger_keywords),
            'judgment': '保守准入；原话证据必须逐字出现在转写里；输出只能写到工作区的 output 目录；文件类任务必须有明确的输入路径。',
            'allowed_task_types': list(gate.allowed_task_types),
            'default_formats': {'document': gate.default_document_format, 'code': gate.default_code_format},
            'control': '模型给出取消或完成信号，同时转写里必须出现对应关键词，而且能唯一对应到已有任务。',
            'cancel_keywords': list(gate.cancel_keywords),
            'completion_keywords': list(gate.completion_keywords),
            'duplicates': '同样的原话 + 同类任务 + 同输出格式视为同一任务，不会重复执行。',
            'retries': retry_policy(settings),
            'screening_only': '只筛选不执行。' if settings.screening.screening_only else '通过筛选和预检后允许执行。',
            'extra_instructions': gate.extra_instructions,
        },
    }


def retry_policy(settings: Settings) -> dict:
    """Both retry layers and the worst-case number of model calls per batch."""
    inner = settings.classifier.max_attempts
    outer = settings.screening.gate.max_classification_attempts
    return {
        'per_call_attempts': inner,
        'per_batch_attempts': outer,
        'retry_delay_seconds': settings.screening.gate.retry_delay_seconds,
        'worst_case_model_calls_per_batch': inner * outer,
        'summary': (
            f'两层重试，都有上限。单次判断内：模型输出不合格时最多试 {inner} 次（上限 5），'
            f'认证失败、额度耗尽等不可重试错误会立即停止。批次级：整次判断失败后，'
            f'每隔 {settings.screening.gate.retry_delay_seconds} 秒再判断一次，最多 {outer} 次（上限 10），'
            f'之后标记为 EXHAUSTED，不再自动重试，需要显式调用 retry_classification。'
            f'因此一批最坏会调用模型 {inner * outer} 次；用订阅时会消耗相应额度。'
        ),
    }
