"""Subscription risk disclaimer (docs/DESIGN.md §3).

The Chinese paragraph is quoted verbatim from the design so a setup guide can
repeat it unchanged; the English paragraph is the same warning for English
speaking users.  ``SUBSCRIPTION_DISCLAIMER`` carries both.
"""

SUBSCRIPTION_DISCLAIMER_ZH = (
    '你选择用个人 ChatGPT/Claude 订阅，以非交互方式自动处理转写。'
    '这可能违反提供商的服务条款或使用政策，会消耗订阅额度，'
    '可能触发限流、封号或其他处置。'
    '本项目与 OpenAI、Anthropic 无关，不作任何保证。'
    '**一切后果由你自行承担。**'
)

SUBSCRIPTION_DISCLAIMER_EN = (
    'You have chosen to use your personal ChatGPT/Claude subscription to process '
    'transcripts in a non-interactive way. This may violate the provider terms of '
    'service or usage policy, will consume subscription quota, and may trigger rate '
    'limiting, account suspension or other action. This project is not affiliated '
    'with, endorsed by or supported by OpenAI or Anthropic and makes no guarantees. '
    '**All consequences are your own responsibility.**'
)

SUBSCRIPTION_DISCLAIMER = f'{SUBSCRIPTION_DISCLAIMER_ZH}\n\n{SUBSCRIPTION_DISCLAIMER_EN}'

__all__ = [
    'SUBSCRIPTION_DISCLAIMER',
    'SUBSCRIPTION_DISCLAIMER_EN',
    'SUBSCRIPTION_DISCLAIMER_ZH',
]
