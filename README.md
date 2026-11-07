<div align="center">

# said-done

### 你说出口，事就办成。

**把语音转写变成经过审查、自动执行的任务 —— 一个开源的 MCP 服务**

[![CI](https://github.com/Jimmy2004/said-done/actions/workflows/ci.yml/badge.svg)](https://github.com/Jimmy2004/said-done/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11–3.13](https://img.shields.io/badge/Python-3.11%E2%80%933.13-blue.svg)](pyproject.toml)

**简体中文** · [English](README.en.md)

</div>

---

**📋 使用要求**

- **一个提供转写文本的语音服务**，包括：
  - **录音硬件**：负责录音并转写成文字。推荐作者目前在用的 **极米记得手环**。
  - **一台 host 主机**：能把转写文本同步给 Agent（例如可参考 [mnemo-push-public](https://github.com/Jimmy2004/mnemo-push-public) 把转写同步到自己的服务器）。
- **至少一个有效的 Agent 订阅**，例如 ChatGPT（Codex）或 Claude（Claude Code）订阅，用来筛选和执行任务。

**🚀 让 Agent 帮你一键配置**：把下面整段复制，粘贴到 Claude Code、Codex、opencode 等能运行命令的 Agent 里即可。

```text
请帮我安装并配置 said-done MCP 服务（https://github.com/Jimmy2004/said-done），然后带我完整走一遍 setup。按顺序执行：

1. 检查环境：确认已安装 git 和 Python 3.11 或更高版本。缺少的话告诉我如何安装，不要擅自安装系统软件。
2. 安装：把仓库克隆到 ~/said-done（若已存在则执行 git pull），在其中创建虚拟环境 .venv，并运行 .venv/bin/python -m pip install -e .
3. 初始化配置：若 ~/.config/said-done/config.json 不存在，就把仓库里的 config.example.json 复制过去（默认使用 mock 模型、只筛选不执行，是安全的）。
4. 注册 MCP：判断你自己运行在哪个宿主里（Claude Code、Claude Desktop、Codex、opencode 或其他），参照仓库 docs/AGENT_SETUP.md 中对应宿主的写法，把 said-done 注册为 stdio MCP 服务：command 用 .venv/bin/said-done-mcp 的绝对路径，环境变量 SAID_DONE_CONFIG 设为上述配置文件的绝对路径。修改已有配置文件前先备份，并告诉我改了哪个文件。
5. 如果需要重启宿主或重新加载 MCP 才能生效，明确告诉我该怎么做，等我确认后再继续。
6. 连接成功后调用 said-done 的 get_setup_guide 工具，严格按指南逐步带我完成：筛选模型 → 批处理与准入规则 → 转写源 → 执行环境 → 先以只筛选模式观察结果。每一步需要我做选择时，停下来问我，不要替我决定。

安全要求：绝对不要读取凭据 txt 文件的内容，也不要让我在对话中粘贴 API key。凭据只能通过 setup_begin 生成的文件由我自己填写，再由 MCP 导入系统钥匙串；请向我解释为什么你看不到 key。
```

said-done 是一个本地运行的 MCP 服务：它读取你的语音转写文本，从中找出你随口提到、真正想做的任务，经过严格校验后交给你选择的 AI 执行环境完成。转写来源、筛选模型和执行环境都可以替换；配置由 MCP 宿主里的 Agent 一步步引导完成。

本项目只处理转写**文本**，不负责录音和语音识别。

## 工作流程

```text
手动输入 / 文件夹 / 转写 MCP 服务 / 第三方转写源插件
                    │
                转写片段
                    │
          SQLite 存储 + 心跳攒批
                    │
          筛选模型判断 + 严格校验
                    │
          审计记录 → 去重 → 任务状态机
                    │
          预检 → 任务工作区 → 执行环境
                    │                  ▲
               产物校验           宿主认领 / 完成
                    │
             执行结果 + 审计

MCP 宿主 ⇄ stdio 工具 / guide://setup 引导 / setup 提示词
                    │
    config.json + 系统钥匙串（机密单独存放）

常驻调度：said-done run（独立的后台进程）
```

## 快速开始

需要 Python 3.11 及以上。离线测试不需要任何 API、账号或真实的 CLI。

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
mkdir -p ~/.config/said-done
cp config.example.json ~/.config/said-done/config.json
.venv/bin/said-done-mcp
# 等价于：.venv/bin/python -m said_done.mcp_server
```

示例配置使用 mock 分类器、手动输入转写和 mock 执行器，并开启 `screening_only=true`（只筛选、不执行）。

接入 MCP 宿主后，让 Agent 调用 `get_setup_guide`，按顺序完成：筛选模型 → 攒批与准入规则 → 转写来源 → 执行环境。建议先只筛选一段时间，确认判断质量后再允许执行。

MCP 进程默认不会自己轮询，常驻运行请另开后台进程：

```sh
.venv/bin/said-done --config ~/.config/said-done/config.json run
```

完整步骤、凭据安全说明、launchd / systemd 模板，以及 Claude Desktop、Claude Code、Codex、opencode 的接入配置，见 [Agent 安装引导](docs/AGENT_SETUP.md)。也可以读取资源 `guide://setup` 或调用提示词 `setup`。

以 Codex 为例（合并进 `~/.codex/config.toml`，并替换绝对路径）：

```toml
[mcp_servers.said-done]
command = "/ABS/PROJECT/.venv/bin/said-done-mcp"
[mcp_servers.said-done.env]
SAID_DONE_CONFIG = "/ABS/CONFIG/config.json"
```

命令行同样可用：`submit`、`process-now`、`status`、`task-result`、`screening-audit` 等。

- **配置文件位置**：MCP 读取 `$SAID_DONE_CONFIG`，其次是 `$SAID_DONE_HOME/config.json`，默认为 `~/.config/said-done/config.json`。命令行可以用 `--config` 显式指定。
- **前后台要一致**：后台进程和 MCP 必须使用同一份配置和同一个数据目录。
- **环境变量**：统一使用 `SAID_DONE_` 前缀。

## 使用方式

- **配置**：`setup_status` → 导入凭据、选择并测试模型 → `explain_screening` → 转写来源 → 执行环境 → 审查筛选结果。
- **运行**：`submit_transcript`、`process_now`、`status`、`list_tasks`、`task_result`。后台进程正在运行时，返回 `{"busy": true}` 属于正常情况。
- **审计**：`audit_summary`、`audit_records`。默认隐藏转写正文和证据字段，显式传 `include_evidence=true` 才会返回。
- **宿主拉取**：执行环境选 `host` 时，调度器完成预检后，宿主用 `claim_next_task` 认领任务，在工作区里完成，再调用 `complete_task` 触发产物校验；做不了就 `release_task` 退回。

**默认的攒批与准入规则**：
- 每 600 秒检查一次。
- 满足任一条件就送去判断：攒满 200 字、出现触发词、最早一条等待超过 1,800 秒。每批最多 50 段。
- 原话证据、输入路径和产物路径都必须通过严格校验。

以上规则都可以用 `configure_screening` 修改；不确定时保留默认值即可。

## 安全模型

**凭据怎么保存**：API key 不会出现在聊天或配置文件里。
1. `setup_begin` 创建一个仅本人可读写的 txt。
2. 用户自己填写 key，由本地 MCP 进程把它导入系统钥匙串，再尽力覆盖并删除这个 txt。
3. Agent 只能拿到文件路径、key 的指纹、长度和导入状态。
4. 钥匙串不可用时，会明确提示改为存成未加密的 0600 文件。

调用 API 时，key 只会发给用户自己配置的 API 地址。

**这不是绝对隔离**：
- 有文件或 shell 权限的 Agent 技术上可以读取那个 txt，安装引导里明确禁止这样做。
- 同一系统用户下的其他程序也可能访问钥匙串。
- 在 SSD / APFS 上，覆盖删除无法保证彻底擦除。
- 如果不小心把 key 贴到了聊天里，请立即去提供商后台换一个新的。

**始终生效的保护**：
- 原话证据必须逐字出现在转写里；产物只能写到工作区的 `output/`。
- 文件类任务要先预检，输入只提供只读副本；执行完成后还要校验产物。
- 任务状态机控制每一步；程序崩溃后不会盲目重跑。
- 报错不会回显转写原文，也不会回显提供商返回的错误正文。

**需要自己留意的边界**：
- CLI 和宿主仍然拥有它们自己的权限。没有系统沙箱的执行环境，即使隔离了工作目录，也不等于有系统级保护。
- 第三方转写源插件是本地运行的 Python 代码，请只安装你信任的插件。

**使用订阅前的免责声明**：使用订阅分类器之前，必须完整展示下面这段声明，取得明确同意后才能设置 `acknowledge_subscription_risk=true`：

> 你选择用个人 ChatGPT/Claude 订阅，以非交互方式自动处理转写。这可能违反提供商的服务条款或使用政策，会消耗订阅额度，可能触发限流、封号或其他处置。本项目与 OpenAI、Anthropic 无关，不作任何保证。**一切后果由你自行承担。**

## 支持情况

“✅ 实测通过”指 2026-11-07 在 macOS 上做过真实的端到端测试，其余为按设计实现的状态。测试时的 CLI 版本：codex-cli 0.166.0、Claude Code 2.1.310、opencode 2.1.3。MCP stdio 链路以及 macOS 钥匙串的导入和删除也已实测。

### 筛选模型

| 分类器 | 接口 | 状态 |
|---|---|---|
| `chat-completions` | `/chat/completions` | ✅ 实测通过（MiniMax）。适用于 MiniMax、DeepSeek、OpenRouter 和各类网关 |
| `openai-compatible` | OpenAI Responses API（`/responses`） | ✅ 实测通过（MiniMax） |
| `anthropic` | Anthropic Messages（`/v1/messages`） | ✅ 实测通过（MiniMax 的 Anthropic 兼容地址） |
| `codex-subscription` | 官方 Codex CLI，决策程序模式 | ✅ 实测通过，默认 `gpt-6-luna`。需本机登录，并确认订阅风险 |
| `claude-subscription` | 官方 Claude CLI | ✅ 实测通过，默认 `claude-haiku-5-5` |
| `mock` | 离线 | 用于演示和测试 |

实测用的是 5 条转写：2 条应批准（一个文档任务、一个代码任务），2 条应拒绝（闲聊、转述别人的话），1 条是同一批里提出后又取消。上表所有分类器都是 5/5。

**API 格式靠探测确定**：导入凭据时，如果没有填写 `api_style`，会向 `/models`、`/chat/completions`、`/responses`、`/messages` 发送不带模型的最小请求来判断，不消耗 token，也不靠网址去猜。
- 多种格式都可用时，按 chat-completions、openai-compatible、anthropic 的顺序选择，并列出其他可选项。
- 如果手动填写了格式，同样会探测验证；不匹配只给出警告。
- 探测失败时，key 仍会保存、txt 照样删除，并提示检查地址和 key 或手动填写格式。之后可以用 MCP 工具 `probe_api` 重新探测。

### 模型怎么选

- **筛选**是轻量的分类任务：
  - 两种订阅默认用最便宜的模型（`gpt-6-luna`、`claude-haiku-5-5`）。
  - 标准 OpenAI / Anthropic API：由 Agent 推荐列表里最便宜的模型，用户确认。
  - 其他提供商：由用户用自己的话说想要哪个模型，Agent 查到准确的模型 ID 后请用户确认。
- **执行**会写出真实的文档和代码：不设默认模型，也不会沿用筛选模型，由用户自己选，也可以选“沿用该执行环境自身的默认模型”。

### Codex 订阅的决策程序模式

模型不靠最后一条消息输出 JSON，而是运行我们提供的一个确定性小程序：
- 没有任务时，运行 `decide.py N "理由"`。
- 有任务时，先写草稿 JSON，再运行 `decide.py submit draft.json`。小程序用正式的校验器检查草稿，不通过就打印修改提示，让模型当场修改，最多 5 次。
- 通过后小程序写出结果文件，服务端读取后再完整校验一次。

所以模型说话再多，也不影响结果。`test_classifier` 会先跑一个 Y/N 冒烟程序，确认 CLI 能正常执行。

**已知限制**：OpenAI 的严格结构化输出不接受当前的任务 schema（其中有可选字段和开放的字典字段）。因此：
- Codex 订阅默认不传 `--output-schema`；`chat-completions` 的 `use_response_format` 也应保持关闭。schema 写在提示词里，由程序校验并重试。
- 内置的 Codex 模型列表只是示例，可以填写任意模型 ID。

### 执行环境

| 执行环境 | 状态 | 隔离与限制 |
|---|---|---|
| `codex-cli` | ✅ 实测通过 | Codex 系统沙箱 |
| `codex-app-server` | ✅ 实测通过 | Codex 系统沙箱 |
| `claude-code` | ✅ 实测通过 | 工具白名单，Bash 默认关闭；没有系统沙箱 |
| `opencode` | ✅ 实测通过（使用已登录提供商的模型）；不建议使用免费模型 | 依靠 opencode 自身的权限系统；没有系统沙箱 |
| `pi` | 实验性，未验证 | 按 Pi coding agent 推断的参数，需要自行验证；没有沙箱 |
| `dsh` | 实验性，未验证 | 工具身份未确定，必须由用户确认命令模板 |
| `custom-command` | 由用户负责验证 | 显式的命令模板；没有系统沙箱 |
| `host` | 协议层已验证 | 由宿主认领和完成任务；安全性取决于宿主 |
| `claude-desktop`、`chatgpt-app` | 实验性，未验证 | `host` 的别名；需要宿主版本支持 MCP，请自行验证 |
| `mock` | 离线 | 生成合成产物，不调用模型 |

未验证的执行环境可能需要先安装相应软件，或修改 `said_done/executor/providers/` 下的实现。ChatGPT 桌面应用没有公开的本地自动化接口，只有在所用版本支持所需的 MCP 连接时，才能走宿主拉取模式。

## 编写转写源插件

第三方包在自己的 `pyproject.toml` 里声明入口点，安装后就会被自动发现：

```toml
[project.entry-points."said_done.sources"]
my-source = "my_package.source:MySource"
```

入口点指向一个 source 类，构造参数为 `(source_id, options, repository)`。插件需要：
- 提供一个 `Options` Pydantic 模型，用于配置发现。
- 实现 `id`、`kind`、`poll(ctx) -> PollResult`、`status() -> dict`、`close()`，以及可选的 `next_deadline(now)`。
- 注意 `status` 的返回值里不能包含转写原文。

框架提供的支持：
- `SourceContext` 提供仓库写入、按来源隔离的键值状态和时钟。
- 片段 ID 按来源分命名空间，不同来源之间不会冲突。
- 任何一个来源返回 `complete=false`，本轮都会推迟判断。

配置示例：

```json
{"id": "notes", "kind": "folder", "options": {"path": "/ABS/TRANSCRIPTS"}}
```

具体以 `list_sources` 返回的选项 schema 和[设计文档](docs/DESIGN.md)为准。不要在插件配置里写入凭据。

## 测试

```sh
.venv/bin/python -m pytest -q
```

工具服务层在 `said_done/mcp_server/services.py`，注册层在 `server.py`。provider、凭据和运行时相关模块都是延迟导入的，单纯导入 MCP 包不会启动后台任务或外部程序。

旧版 `minimax` 分类器配置和 `MINIMAX_API_KEY` 环境变量仍然可用；凭据模板中不再提供这个选项。

## 设计思路：让 AI 跑程序，而不是相信它的输出

> AI 时代，点子比代码更重要。

做这个项目时遇到的最大麻烦，不是代码怎么写，而是 **AI 模型不听话**：明明要求只输出一段 JSON，它却会加解释、改字段、漏格式，总有预期以外的结果。传统做法是不断加强提示词、再去解析和修补它的输出，但这本质上是在跟模型的"说话习惯"较劲，永远修不完。

换个角度想：**无论模型嘴上说什么，它的目标始终是"把任务完成"。** 那就别让它"告诉我答案"，而是让它"运行一个程序来交答案"：

- 给它一个很小的、确定性的程序（本项目里是 `decide.py`）。没有任务就运行 `decide.py N "原因"`；有任务就写一个草稿，再运行 `decide.py submit draft.json`。
- 程序当场校验草稿。不合格就打印具体错误，模型为了"完成任务"会自己读报错、修改、重交（有次数上限）。
- 通过后由程序写出结果文件，服务端读取文件并再做一次完整校验。

这样一来，模型说多少废话都无所谓：**最终结果来自程序，而不是来自模型的输出文本**。不可控的"语言输出"被转换成了可控的"程序执行"。正式使用前，还会先让模型跑一个只回答 Y/N 的小程序，确认它确实能执行命令。

这个思路不局限于本项目。只要遇到"模型总是输出格式不对、内容跑偏"的问题，都可以试试：**把要求写成程序，让模型去跑通它，而不是去说服它。**

## 致谢与参考

作者在自己的工作环境中使用 **极米记得** 手环作为录音转写设备，said-done 最初就是为处理它产生的转写文本而写的。本项目与极米科技无任何关联，也不依赖任何特定设备：只要能产出转写文本，任何来源都可以通过转写源插件接入。

本仓库由私人项目转为公开。作者已尽量清理与具体设备相关的命名，但无法保证完全洗干净：部分变量名、注释或配置项可能仍沿用与之相关的描述，这些都不影响使用。

如果你想参考如何在公网环境下提取并备份极米记得 App 的转写文本，可以参见作者的另一个仓库：[Jimmy2004/mnemo-push-public](https://github.com/Jimmy2004/mnemo-push-public)。

## 许可证

[MIT](LICENSE)
