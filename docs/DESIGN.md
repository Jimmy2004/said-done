# 架构设计 v0.2：可插拔转写源 · 任意模型筛选 · 多环境执行 · MCP Server

本文是实现规格。代码标识符用英文；面向用户的文案由 Agent 按用户语言转述。

## 0. 目标与约束

- 输入只有一种：**语音转写文本**（`TranscriptSegment`）。来源做成插件。
- 筛选模型：任意提供商、任意模型；另支持 ChatGPT / Claude **订阅**（经官方 CLI），需用户显式确认风险。
- 攒批与准入规则全部可配置；当前硬编码值即为默认值。
- 执行环境多 provider；setup 时由 Agent 问用户“在哪个环境里执行”。
- 对外提供 **MCP server**（stdio），setup 由宿主 Agent 通过 MCP 工具引导完成。
- 现有安全性质不得退化：证据逐字校验、输出路径校验、状态机、崩溃后不自动重跑、预检、产物校验、错误不回显转写原文。
- 现有 CLI（`python -m said_done.main ...`）继续可用。Python 包名为 `said_done`。

## 1. 配置（Settings v2）

配置目录：`$SAID_DONE_HOME`，缺省 `~/.config/said-done/`（目录权限 0700）。
- `config.json`：非机密配置（`--config` 仍可指定其他路径）。
- `data/`：SQLite、任务工作区、日志（取代默认 `.runtime/`；`data_dir` 仍可配置）。
- `inbox/`：凭据导入用的临时 txt（见 §2）。
- `secrets/`：仅在没有系统钥匙串时使用的回退存储。

```jsonc
{
  "schema_version": 2,
  "data_dir": "~/.config/said-done/data",
  "sources": [ { "id": "manual", "kind": "manual", "options": {} } ],
  "classifier": {
    "kind": "openai-compatible",      // openai-compatible | anthropic | minimax | codex-subscription | claude-subscription | mock
    "profile": "default",             // 凭据 profile 名
    "base_url": "https://api.openai.com/v1",
    "model": "…",
    "effort": "low",                  // none|minimal|low|medium|high|xhigh 或 null（不发送）
    "extra_body": {},                 // 高级：原样合并进请求体，用于提供商私有参数
    "timeout_seconds": 60,
    "max_attempts": 3,
    "acknowledge_subscription_risk": false
  },
  "screening": { /* §4 */ },
  "executor": {
    "kind": "codex-cli",              // §6
    "model": null, "effort": null,
    "timeout_seconds": 900,
    "options": {},                    // provider 私有选项
    "allowed_input_roots": [],
    "preflight": { "max_scan_files": 5000, "max_scan_depth": 4, "max_scan_seconds": 10 }
  }
}
```

- 旧版扁平配置（v1 字段：`transcript_source`、`semantic_mode`、`executor_mode`、`min_content_chars` 等）必须能被加载并映射到 v2；`SAID_DONE_*` 环境变量覆盖保留。
- 旧 `MINIMAX_API_KEY` 环境变量仍可用，映射为 `kind=minimax`。
- 写配置用原子写（临时文件 + `os.replace`），权限 0600。

## 2. 凭据：txt 导入 → 系统钥匙串 → 删除

### 流程
1. Agent 调用 `setup_begin(profile)`。服务端在 `inbox/credentials-<profile>.txt` 创建模板（0600，目录 0700），**只返回路径**。模板：
   ```
   # 填好后保存，然后告诉 Agent “填好了”。不要把 key 发到聊天里。
   # 以 # 开头的行会被忽略。
   base_url =
   api_key =
   # 可选：openai-compatible | anthropic | minimax（留空则自动判断）
   api_style =
   ```
2. 用户在自己的编辑器里填写。
3. Agent 调用 `import_credentials(profile)`。**由 MCP 服务进程**读文件：
   - 拒绝符号链接（`O_NOFOLLOW`）；拒绝非本人所有的文件；目录或文件权限过宽时仍导入，但返回警告。
   - 解析 `key = value`；`base_url` 必须是 https，`localhost`/`127.0.0.1`/`[::1]` 可以用 http。
   - `api_key` 存进系统钥匙串：用 `keyring`（macOS 钥匙串、Windows 凭据管理器、Linux Secret Service），service=`said-done`，username=`<profile>`。
   - 没有可用钥匙串后端时，回退为写 `secrets/<profile>.key`（0600），返回值明确写 `backend: "file-0600-unencrypted"` 和警告。
   - `base_url`、`api_style` 写入 `config.json`。
   - 删除原文件：先用同长度零字节覆盖并 fsync，再 unlink。返回值注明“APFS/SSD 上覆盖只是尽力而为”。
4. 返回值只有：`{stored, profile, backend, key_fingerprint: "sha256:<前8位hex>", key_length, base_url, api_style, file_deleted, warnings}`。**任何返回值、异常、日志都不得包含 key 或文件原文**。解析失败时只说哪一行、哪个字段有问题，不回显该行内容。

### 运行时
- key 只在服务进程内存里，用 pydantic `SecretStr` 保存，发请求时才取值放进请求头。
- `SAID_DONE_API_KEY_<PROFILE大写>` 环境变量可覆盖，用于 CI。
- `credential_status(profile)` 只返回是否存在、后端、指纹。`delete_credentials(profile)` 删除。

### Agent 必须告诉用户的说明（写进 setup guide，按用户语言转述）
- 为什么 Agent 看不到 key：Agent 只是把**文件路径**交给 MCP 工具，读文件发生在**本地 MCP 服务进程**里；工具返回给 Agent 的只有指纹和长度，所以 key 不会进入模型上下文，也不会发到模型提供商。
- key 存在哪：系统钥匙串（或回退的 0600 文件）。txt 导入后会被覆盖并删除。
- 坦白说明局限：
  ① 有文件读取或 shell 能力的 Agent 在技术上**能**打开这个 txt。guide 明确禁止这样做，用户也可以填好后立刻让 Agent 导入，缩短文件存在的时间。
  ② 本机以你的用户身份运行的程序可能访问钥匙串条目（macOS 会弹窗授权）。
  ③ 在 SSD/APFS 上覆盖删除只是尽力而为。
- 千万不要把 key 贴进聊天；如果用户贴了，Agent 应提醒用户去提供商后台轮换这个 key。

## 3. 筛选模型 provider（classifier）

把“准入逻辑”和“调用模型”分开：
- `said_done/semantic/gate.py`：构建 prompt、解析 JSON、去掉 ``` 包裹、调用 `validate_response`、按 schema 重试。只依赖 `ModelProvider`。
- `said_done/providers/`：`ModelProvider` 协议：
  ```python
  class ModelProvider(Protocol):
      kind: str
      def list_models(self) -> list[ModelInfo]: ...   # ModelInfo(id, display_name|None, supports_effort|None)
      def complete(self, messages: list[dict], *, model: str, effort: str | None,
                   json_schema: dict | None, max_output_tokens: int) -> str: ...
      def close(self) -> None: ...
  ```
- 实现：
  | kind | 请求 | 模型列表 | effort 映射 |
  |---|---|---|---|
  | `openai-compatible` | `POST {base_url}/chat/completions`，Bearer | `GET {base_url}/models` | `reasoning_effort`；`null` 时不发送 |
  | `anthropic` | `POST {base_url}/v1/messages`，`x-api-key` + `anthropic-version` | `GET {base_url}/v1/models`（分页） | `output_config.effort`（可经 `extra_body` 覆盖）；`none` 不发送 |
  | `minimax` | `openai-compatible` 的预设，默认 base_url `https://api.minimaxi.com/v1`；保留现有 `base_resp.status_code` 业务码处理（1004/1008/2049/2056 不重试） | 同左；失败时回退到内置列表 | 同现有 |
  | `codex-subscription` | 在空临时目录跑 `codex exec --sandbox read-only --skip-git-repo-check --ephemeral --output-schema <tmp> -o <tmp> [-m M] [-c model_reasoning_effort=E] -`，prompt 走 stdin | 内置列表 + “default（用 CLI 默认）” + 允许自填 | `model_reasoning_effort` |
  | `claude-subscription` | 在空临时目录跑 `claude -p --output-format json [--model M] [--effort E]`，不放行任何工具，prompt 走 stdin，解析 JSON 的 `result` 字段 | 内置别名 `opus` / `sonnet` / `haiku` / default + 允许自填 | `--effort` |
  | `mock` | 现有 MockSemanticAdapter | — | — |
- `api_style` 自动判断：base_url 含 `anthropic.com` → anthropic；含 `minimax` → minimax；其他 → openai-compatible。
- 订阅 provider：`acknowledge_subscription_risk` 不是 true 时，构造直接报错。免责声明（`SUBSCRIPTION_DISCLAIMER` 常量，guide 原样引用）：
  > 你选择用个人 ChatGPT/Claude 订阅，以非交互方式自动处理转写。这可能违反提供商的服务条款或使用政策，会消耗订阅额度，可能触发限流、封号或其他处置。本项目与 OpenAI、Anthropic 无关，不作任何保证。**一切后果由你自行承担。**
- 订阅 CLI 的子进程：要设超时、杀进程树（复用 `said_done/executor/process_control.py`）；stderr 不写进错误信息，只记异常类型和退出码。
- 现有 `MiniMaxAdapter` 行为保持（测试保留），内部改用新结构。

## 4. 攒批与准入规则（全部可配置）

```jsonc
"screening": {
  "screening_only": true,                 // 新 setup 默认只筛选不执行，用户确认后再关
  "batch": {
    "heartbeat_interval_seconds": 600,
    "min_content_chars": 200,
    "trigger_keywords": ["我要","我需要","帮我","请帮","取消","撤销","已经完成","已经做完"],
    "max_batch_wait_seconds": 1800,
    "max_batch_segments": 50
  },
  "gate": {
    "allowed_task_types": ["CREATE_DOCUMENT","CONVERT_FILE","WRITE_CODE","MODIFY_FILE"],
    "cancel_keywords": ["取消","撤销","cancel"],
    "completion_keywords": ["已完成","已经完成","已经做完","做完了","completed"],
    "default_document_format": "docx",
    "default_code_format": "py",
    "extra_instructions": "",             // 用户自定义准入偏好，追加到 system prompt 末尾（作为用户策略，不能放宽安全校验）
    "max_classification_attempts": 3,
    "retry_delay_seconds": 30
  }
}
```

- `should_process`、`validate_response`、`SYSTEM_PROMPT` 都改为从配置读取；默认值必须与现有行为逐字一致（现有测试应原样通过）。
- `allowed_task_types` 之外的 APPROVE 在校验时拒绝。
- `explain_screening()` 返回 `{values, defaults, explanation}`。`explanation` 是一段结构化说明，供 Agent 转述：
  - 什么时候送去判断：满足任一条件——内容攒够 N 字；出现触发词；最早一条等待超过 T 秒。
  - 模型怎么判断：保守准入；原话证据必须逐字出现在转写里；输出只能写到工作区的 output 目录；文件类任务必须有明确的输入路径。
  - 取消/完成怎么认：模型给出信号，同时转写里必须出现对应关键词，而且能唯一对应到已有任务。
  - 重复怎么处理：同样的原话 + 同类任务 + 同输出格式视为同一任务，不会重复执行。
  - 失败重试：最多 N 次，间隔 S 秒；用完后要显式调用 `retry_classification`。
- `configure_screening(patch)`：按 JSON merge-patch 部分更新，返回新值和与默认值的差异；`reset_screening_defaults()`。

## 5. 转写源插件

```python
class TranscriptSource(Protocol):
    id: str; kind: str
    def poll(self, ctx: SourceContext) -> PollResult: ...   # PollResult(segments: list[TranscriptSegment], complete: bool)
    def next_deadline(self, now: float) -> float | None: ... # 可选：自带调度（如对齐服务器同步时间）
    def status(self) -> dict: ...                            # 不得含转写原文
    def close(self) -> None: ...
```
- `SourceContext` 提供 `repo.ingest`、按 `(source_id, key)` 存取的 KV 状态（新表 `source_state`），以及时钟。
- 注册表：内置 kind + Python entry points 组 `said_done.sources`（第三方插件只需 `pip install`）。`list_sources()` 列出可用 kind 和各自的 options schema（pydantic 模型的 JSON schema）。
- 内置：
  - `manual`：纯被动，靠 `submit_transcript` 工具或 CLI `submit` 推入。
  - `folder`：监视一个目录，读取新增的 `.txt`（整份当一段）和 `.json`（segment 或数组）；处理后移进 `processed/`，按“文件路径 + 内容 sha256”去重。
  - `mcp-stdio`：通过 stdio MCP 连接自行托管的转写服务，提供 `get_transcripts` / `sync_status`。options：`command`（argv 数组）、`device`、`transcript_tool`（默认 `get_transcripts`）、`status_tool`（默认 `sync_status`，可为 null）、`poll_interval_seconds`。预设 `cloud-transcript-sync` 等价于现有行为（游标、对账、调度原样保留，现有测试保留）。
- 可以配置多个 source；Scheduler 每个 tick 依次 poll，任何一个返回 `complete=False` 时本轮不进入分类（保持“窗口不完整就不判断”的语义）。
- `segment_id` 用 `source_id` 作命名空间，避免不同来源撞 ID。

## 6. 执行 provider

继续沿用 `Executor` 协议。预检、工作区准备（输入复制成只读副本、输出目录）、产物校验、状态机与 provider 无关，所有 provider 共用。新增：

```python
class ExecutorProvider(Executor, Protocol):
    kind: str
    verification: Literal["verified", "experimental-unverified"]
    @classmethod
    def detect(cls) -> DetectResult: ...   # installed, path, version, notes, install_hint
```

| kind | 方式 | 沙箱 | 状态 |
|---|---|---|---|
| `codex-cli` | 现有 `codex exec -C <ws> -s workspace-write` | Codex OS 沙箱 | verified |
| `codex-app-server` | 现有 app-server | Codex 沙箱 | verified |
| `claude-code` | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools Read,Write,Edit,Glob,Grep [--model] [--effort]`，cwd=工作区，prompt 走 stdin；Bash 默认关闭（`options.allow_bash`） | 无 OS 沙箱，靠工具白名单 + 工作区隔离 | verified（本机已安装；以实测为准） |
| `opencode` | `opencode run --format json [--model provider/model] --auto <prompt>`，cwd=工作区 | 无 OS 沙箱，靠 opencode 权限系统 | verified（本机已安装；以实测为准） |
| `pi` | 假设为 Pi coding agent（`pi -p <prompt> [--provider] [--model]`），cwd=工作区 | 无 | experimental-unverified |
| `dsh` | 不清楚具体是哪个工具：做成 `custom-command` 的预设，命令模板需要用户确认 | 无 | experimental-unverified |
| `custom-command` | `options.argv` 模板，支持占位符 `{workspace}` `{prompt}` `{prompt_file}` `{model}` `{effort}`；`options.prompt_via` = `stdin`/`arg`/`file` | 无 | 由用户负责 |
| `host` | **宿主拉取模式**：任务停在 `RUNNING` 等宿主认领。宿主 Agent（Claude Desktop、ChatGPT App 或任何 MCP 宿主）调用 `claim_next_task` 拿到任务包和工作区路径，用自己的工具完成后调用 `complete_task(task_id, summary)`，服务端跑产物校验再落状态 | 取决于宿主 | verified（协议层） |
| `claude-desktop` | `host` 的别名，附带 Claude Desktop 配置说明 | 宿主 | experimental-unverified（需要用户实测） |
| `chatgpt-app` | `host` 的别名。ChatGPT 桌面应用没有公开的本地自动化接口，只能通过 MCP（如果所用版本支持本地 MCP / 开发者模式）走宿主拉取 | 宿主 | experimental-unverified |
| `mock` | 现有 | — | — |

- `host` 模式状态细节：`PREFLIGHT → RUNNING` 后不阻塞调度器，`run_queued` 跳过 `RUNNING` 的 host 任务。认领有租约（`claimed_at`，默认 2 小时）。`recover()` 对 host 任务不判失败，只对过期租约记事件，等待重新认领。状态机若需新增转移，必须写测试。
- 无 OS 沙箱的 provider：`detect()` 的 notes 和 `configure_executor` 的返回值都要带警告，Agent 必须转述。
- `list_executors()` 返回每个 kind 的 `detect()` 结果、verification、sandbox 说明、install_hint、options schema。
- unverified provider 被选中时，返回 `needs_user_verification: true` 和说明：命令行参数是根据公开文档推断的，需要你在本机实测；可能需要先安装，或者改一下 `said_done/executor/providers/<kind>.py`。
- 子进程统一走 `process_control`：超时、取消、杀进程树；stdout/stderr 设上限；错误信息不回显 stderr 全文（截断，且不含 prompt）。

## 7. MCP Server

- 依赖官方 `mcp` Python SDK（`FastMCP`），stdio 传输。入口：`said-done-mcp`（`said_done.mcp_server:main`），也支持 `python -m said_done.mcp_server`。
- server `instructions`：一句话说明用途；首次使用先调 `get_setup_guide`，并遵守其中的安全规则。
- 资源：`guide://setup`（= `docs/AGENT_SETUP.md`）。Prompt：`setup`（返回同样的引导）。
- 工具（全部返回 JSON 可序列化的 dict，不返回机密）：
  - **Setup**：`get_setup_guide`、`setup_status`（每个环节完成没有、下一步做什么）、`setup_begin`、`import_credentials`、`credential_status`、`delete_credentials`、`list_models`、`configure_classifier`（订阅类必须带 `acknowledge_subscription_risk=true`）、`test_classifier`（用内置的合成转写跑一次，返回判定摘要）、`explain_screening`、`configure_screening`、`reset_screening_defaults`、`list_sources`、`configure_sources`、`list_executors`、`configure_executor`、`test_executor`（mock 任务或 dry-run：只跑 detect + 预检）。
  - **运行**：`submit_transcript(text, segment_id?, started_at?, ended_at?, source?)`、`process_now`、`status`、`list_tasks(state?, limit)`、`task_result(task_id)`、`cancel_task(task_id)`（仅 QUEUED/PREFLIGHT）、`audit_summary`、`audit_records(... , include_evidence=false)`（默认不返回转写原文和证据引用，只给 ID、动作、理由）、`retry_classification`、`source_status`。
  - **宿主拉取**：`claim_next_task`、`complete_task`、`release_task`。
- 并发：工具里的 `process_now` 与后台 `said-done run` 共用 `worker.lock`。锁被占用时返回 `{busy: true}`，不报错。
- MCP server 默认**不在进程内常驻轮询**（宿主会随时启停它）。`setup_status` 会提示用户用 `said-done run` 常驻，并给出 launchd / systemd 示例（写在 guide 里）。可选配置 `run_scheduler_in_server`。
- 所有 schema 变更（新表、新列）都要做幂等迁移。

## 8. Agent setup guide（docs/AGENT_SETUP.md）

用英文写，开头要求 Agent 用用户的语言交流。按顺序：
1. `setup_status`；说明本工具是做什么的。
2. **筛选模型**：问用户用 API（任意提供商）还是订阅（ChatGPT 或 Claude）。
   - 订阅：原样展示免责声明，拿到明确同意后再 configure。
   - API：`setup_begin` → 告诉用户文件路径，让用户自己填 → 转述 §2 的安全说明 → 用户说填好后 `import_credentials` → `list_models` → **主动问**用户选哪个模型、用什么 effort（推荐值：筛选默认 `low`；用户不确定就用推荐值）→ `configure_classifier` → `test_classifier`。
   - 硬规则：不读那个 txt，不在聊天里索要 key，不复述任何像 key 的字符串。
3. **攒批与准入**：`explain_screening`，用大白话把判断逻辑讲给用户听；问要不要改；用户不确定就保留默认值。
4. **转写源**：`list_sources`，问用户转写从哪来；默认 `manual`。
5. **执行环境**：直接问“在哪个环境里执行？”，列出 `list_executors` 的结果并标出本机已安装的；选中 unverified 的要明确告知需要自行验证，可能要自己装或改代码；无 OS 沙箱的要提示风险；`host`/`claude-desktop`/`chatgpt-app` 要说明拉取模式怎么用。
6. 先保持 `screening_only=true` 跑一段时间，用 `audit_summary` 检查判断质量，满意后再关。
7. 常驻运行的方法；各宿主的 MCP 配置示例（Claude Desktop、Claude Code、Codex、opencode）。

## 9. 测试与验收

- 现有 102 个测试全部通过，包括修好的 2 个：Mock 分类器不能依赖本机 `~/Desktop` 里有没有文件；测试用 xlsx 补上 `[Content_Types].xml`，或者让读取逻辑不再要求它（二选一，以真实 Excel 兼容为准）。
- 新增测试（不访问网络，不调用真实 CLI）：
  - 凭据导入：用内存 keyring 后端。验证符号链接被拒；导入后文件被删除；返回值和异常文本里都找不到 key；回退文件权限是 0600。
  - provider：用 `httpx.MockTransport` 测 openai-compatible / anthropic / minimax 的请求体、effort 映射、`list_models`。
  - 订阅 provider：用假 CLI 脚本，参照 `tests/fake_codex_cli.py`。验证未确认风险时拒绝构造。
  - 筛选配置：改了配置后 `should_process`、validator、prompt 都随之变化；默认值与旧行为一致。
  - source 插件：注册表、entry point 加载、folder 源去重、多源时 complete 语义。
  - 执行 provider：每个 CLI 型 provider 用假可执行文件测参数拼装和输出解析；host 模式完整走一遍认领 → 完成 → 校验 → 过期租约。
  - MCP：在进程内调用 FastMCP 工具，走完 setup 流程（mock classifier + mock executor），并断言所有工具输出里都不含测试 key。
- `pyproject.toml` 新增依赖：`mcp`、`keyring`；新增入口 `said-done-mcp`。
- README 更新：项目定位改为 MCP server；快速开始；各宿主接入方式；安全模型。

## 10. 并行工作流与接口约定（实现期）

`said_done/config.py`（Settings v2 + `load_settings`/`save_settings`/`patch_section`/`merge_patch`/`config_home`/`default_config_path`）已经定稿，**任何工作流都不要修改**。如需新字段，在交付说明里提出来，由总控统一加。

### 文件归属（只改自己负责的文件；需要改别处时写进交付说明）
- **A · 模型与凭据与筛选**：`said_done/credentials/**`、`said_done/providers/**`、`said_done/semantic/**`、`said_done/heartbeat/threshold.py`；测试 `tests/test_credentials*.py`、`tests/test_providers*.py`、`tests/test_semantic*.py`、`tests/test_screening_config*.py`。
- **B · 转写源与执行与运行时**：`said_done/ingestion/**`、`said_done/executor/**`、`said_done/broker/**`、`said_done/storage/**`、`said_done/runtime.py`、`said_done/heartbeat/scheduler.py`、`said_done/main.py`；测试 `tests/test_ingestion*.py`、`tests/test_sources*.py`、`tests/test_executor*.py`、`tests/test_host*.py`、`tests/test_core_slice.py`、`tests/test_incremental_screening.py`、`tests/test_screening_audit.py`、`tests/test_cloud_*.py`、`tests/test_cloud_mcp_client.py`、`tests/fake_*`。
- **C · MCP 与文档**：`said_done/mcp_server/**`、`docs/AGENT_SETUP.md`、`README.md`、`pyproject.toml`、`config.example.json`、`.env.example`；测试 `tests/test_mcp*.py`。

### 各工作流对外提供的函数（签名固定；C 按这些签名调用）
A 提供：
- `said_done.credentials.begin_import(settings, profile='default') -> dict`
- `said_done.credentials.import_credentials(settings, profile='default') -> dict`：只负责存 key、删文件。返回 dict 里的 `updated_classifier` 是 classifier 段的 patch（base_url、kind 等），由调用方用 `patch_section` + `save_settings` 写回配置；函数本身不写 config.json。
- `said_done.credentials.credential_status(settings, profile='default') -> dict`
- `said_done.credentials.delete_credentials(settings, profile='default') -> dict`
- `said_done.credentials.get_api_key(settings, profile='default') -> pydantic.SecretStr | None`
- `said_done.providers.SUBSCRIPTION_DISCLAIMER: str`
- `said_done.providers.build_model_provider(settings) -> ModelProvider`
- `said_done.providers.list_models(settings) -> list[dict]`（按 settings.classifier 当前的 kind/base_url/profile）
- `said_done.semantic.factory.build_classifier(settings, repository) -> classifier`（`.classify(batch)`；保留 `.model` 属性，供审计记录模型名）
- `said_done.semantic.factory.test_classifier(settings) -> dict`（用内置合成转写、临时内存仓库跑一次）
- `said_done.semantic.screening.explain_screening(settings) -> dict`
- `said_done.semantic.validator.validate_response(response, batch, known_tasks=None, gate=None)`（`gate: GateConfig | None`，None 等于默认值）
- `said_done.heartbeat.threshold.should_process(segments, min_content_chars=200, max_batch_wait=1800, now=None, keywords=None)`

B 提供：
- `said_done.ingestion.registry.list_source_kinds() -> list[dict]`；`build_sources(settings, repository) -> list[TranscriptSource]`
- `said_done.executor.registry.list_executors() -> list[dict]`；`build_executor(settings) -> Executor`；`executor_info(kind) -> dict`
- `said_done.broker.host_queue.claim_next_task(repository, lease_seconds=7200) -> dict | None`；`complete_task(repository, task_id, summary, validate=True) -> dict`；`release_task(repository, task_id) -> dict`
- `Repository.list_tasks(state=None, limit=50) -> list[dict]`；`Repository.cancel_task(task_id) -> dict`
- `said_done.runtime.create_runtime(settings) -> Scheduler`（`.tick(force)`、`.close()`、`.repository`、`.broker`）；Scheduler 用 `should_process(..., keywords=settings.screening.batch.trigger_keywords)` 和 `validate_response(..., gate=settings.screening.gate)`，分类器通过 `build_classifier` 获取。

## 11. 模型选择规则（v0.2.1）

| 用途 | 提供商 | 默认 | 谁决定 |
|---|---|---|---|
| 筛选 | ChatGPT 订阅（`codex-subscription`） | `gpt-6-luna`（最便宜；已实测可用，`gpt-6.1-luna` 走 ChatGPT 账号会被拒） | 默认值，用户可改 |
| 筛选 | Claude 订阅（`claude-subscription`） | `claude-haiku-5-5`（最便宜；已实测可用） | 默认值，用户可改 |
| 筛选 | 标准 OpenAI / Anthropic API | 无硬编码；Agent 从 `list_models` 里推荐最便宜的（标有 `recommended_for_screening` 的优先），用户确认 | Agent 推荐，用户确认 |
| 筛选 | 其他提供商（MiniMax、OpenRouter、网关、本地模型…） | 无 | 用户用自己的话说想要哪个，Agent 负责查到准确的模型 ID 并请用户确认 |
| 执行 | 所有执行环境 | **无默认，不用最便宜的** | 用户选择；可以选“沿用该环境自身的默认模型” |

- openai-compatible / anthropic 没有配置模型时直接报错，并提示调用 `list_models`；不再发送占位值 `default`。
- 筛选模型不会被沿用到执行环境。

## 12. Codex 订阅的“决策程序”模式（v0.2.1）

`classifier.cli_response_mode = "program"`（默认）。不再从 Codex 的最后一条消息里解析 JSON，而是让模型运行我们提供的确定性小程序：
- `decide.py N "理由"`：没有任务时用这个。
- `decide.py submit draft.json`：用与正式流程相同的校验器检查草稿；不通过就打印修改提示，最多 5 次。
- 通过后小程序写出 `final.json`，服务端读取后**再做一次完整校验**（以服务端为准；模型绕过小程序直接写文件也得不到额外权限）。
- Codex 在空临时目录里运行，使用 `workspace-write` 沙箱，默认不能联网。
- `test_classifier` 先跑 Y/N 冒烟程序（`yn.py`），证明 CLI 能执行，再做真实判断。
- `final-message` 模式保留旧行为。
