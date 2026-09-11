# Credra Agent

全服务日志、启动归档、10 MB 轮转和日志故障后的安全恢复，见[日志使用说明](docs/全服务日志使用说明.md)。当前开发进度与验证记录见 [V2-1 实施记录](docs/V2-1%20实施与验证记录.md)。

架构优化方向见 [自主调查架构与演进路线 v2.0](docs/Credra%20Agent%20自主调查架构与演进路线%20v2.0（提案）.md)。V2-1 已实现自然语言 TaskSpec、完整 MCP 查询参数、LLM Coordinator、预算/执行账本、版本化恢复和 shadow 对照链路；baseline 仍是默认路径，复杂案例深化属于 V2-2。首页隐藏 `case_normal` 和 `case_risky` 两个合成技术样例；比亚迪、上汽继续展示，隐藏样例的源资料和回归用途保留。

Credra Agent 是一个以企业授信尽调为业务载体的长任务 Agent MVP。项目重点不是构建银行级风控模型，而是验证一套可暂停、可恢复、可人工介入、可调用确定性工具并能追踪执行状态的 Agent Runtime。

核心能力：

- Runtime State 驱动的动态执行路径；
- Python 财务计算与规则异常检测；
- Artifact 与 Graph State 分离；
- Research Agent 通过 MCP 调用外部调查工具；
- 规则化 Investigation Intent 与可审计 Query Plan；
- 可插拔 Tavily / Snapshot / Mock 搜索、受控正文抓取与事实核验；
- Verified Fact 到外部风险项的类别化映射；
- 同一 Case 下按任务 Run ID 隔离 Artifact 与报告；
- SQLite Checkpoint 与跨进程 Resume；
- Human-in-the-loop 风险审核；
- Tool Retry、确定性故障注入与 JSONL Trace；
- CLI 可靠入口与包含流程、Evidence、Trace、图表和 HITL 的 Chainlit 场景化工作台。

> Credra Agent 只提供授信尽调辅助分析，不自动批准、拒绝贷款或生成授信额度。

## 1. 运行路径

正常 Case：

```text
Document → Financial → Risk → Report
```

风险 Case：

```text
Document → Financial → Research(MCP) → Risk → Human Review
                                                    ├─ Approve  → Report
                                                    └─ Research → Research vN
                                                                  → Risk vN
                                                                  → Human Review
```

执行路径由 `anomaly_flags`、`risk_level` 和 `human_decision` 决定，不由 LLM 自由规划。

## 2. 系统架构

```mermaid
flowchart TD
    User["User / Reviewer"] --> CLI["Task CLI"]
    User --> UI["Chainlit UI"]
    CLI --> Runtime["Durable Task Runtime"]
    UI --> Runtime
    Runtime --> Graph["LangGraph Workflow"]
    Runtime --> Checkpoint[("SQLite Checkpoint")]
    Graph --> Document["结构化输入校验与归一化"]
    Document --> Financial["Financial Agent"]
    Financial --> PythonTool["Python Financial Tool"]
    Financial --> Route{"Anomaly flags?"}
    Route -->|No| Risk["Risk Agent"]
    Route -->|Yes| Research["Research Agent"]
    Research --> Intent["Investigation Intent / Query Plan"]
    Research --> MCPClient["MCP Client"]
    MCPClient --> MCPServer["Research MCP Server"]
    MCPServer --> Provider["Tavily / Snapshot / Mock"]
    Provider --> Fetcher["受控正文抓取 / Snapshot"]
    Fetcher --> Verifier["Rules / LLM Verifier"]
    Verifier --> Research
    Research --> ResearchExpression["受约束 LLM Evidence Summary / Query Proposal"]
    ResearchExpression --> Risk
    Risk --> Narrative["受约束 LLM 风险解释 / 确定性降级"]
    Narrative --> Review{"MEDIUM / HIGH?"}
    Review -->|No| Report["Report"]
    Review -->|Yes| Interrupt["interrupt / Human Review"]
    Interrupt -->|Approve| Report
    Interrupt -->|Research| Research
    Graph --> Artifacts[("Artifact Store")]
    Runtime --> Trace["JSONL Trace"]
```

### 跨进程恢复时序

```mermaid
sequenceDiagram
    participant U as User
    participant P1 as Python Process A
    participant DB as SQLite
    participant P2 as Python Process B

    U->>P1: start(thread_id, case_risky)
    P1->>DB: persist checkpoints
    P1-->>U: WAITING_APPROVAL + risk details
    Note over P1: Process exits completely
    U->>P2: resume(thread_id, approve)
    P2->>DB: load latest checkpoint
    DB-->>P2: state + pending interrupt
    P2->>DB: persist resumed state
    P2-->>U: COMPLETED + report
```

### MCP边界

```mermaid
flowchart LR
    Agent["Research Agent"] --> Client["FastMCP Client"]
    Client -->|"stdio / MCP"| Server["Research MCP Server process"]
    Server --> Company["search_company(categories)"]
    Server --> Industry["search_industry(categories)"]
    Company --> Providers["Tavily / Snapshot / Mock"]
    Industry --> Providers
    Providers --> Content["Content Fetcher"]
    Content --> Verify["Claim Verifier"]
```

默认情况下无需提前手动启动 MCP Server。风险 Case 进入 Research 节点时，Client 会通过以下模块命令自动启动独立 stdio 子进程：

```powershell
python -m app.mcp.research_server
```

## 3. 技术原则

| 工作 | 实现方式 |
|---|---|
| 财务指标计算 | Python |
| 异常检测 | 配置化规则 |
| Graph Routing | 纯函数 |
| 数据结构约束 | Pydantic |
| 长任务持久化 | LangGraph + SQLite |
| 外部调查 | MCP |
| 中间结果 | Versioned Artifacts |
| 人工审核 | Dynamic interrupt |
| 执行记录 | JSONL Trace |
| LLM 表达 | 受约束的 JSON 输出 + 引用/状态校验 |

财务计算、异常检测、Verified Fact、Risk 等级、Graph Routing 和报告结构保持可离线回归的确定性基线。M4-A 在 Risk 节点后接入受约束的 OpenAI-compatible LLM 风险解释；M4-B 在 Research 节点内基于已形成的结构化证据索引生成 Evidence Summary 与仅供人工审核的 Query Proposal；M4-C2 在 Report 节点使用独立 Prompt/Schema 生成带引用的 Executive Summary 与固定类别报告段落；M4-C1 再对全部模型表达执行引用、来源指纹、事实、数字与 URL 校验，只把通过项投影到最终报告。模型没有 Tool 权限，不能新增事实、提升 Evidence 状态、修改规则 Query Plan、风险等级、路由或替代人工审核；模型、配置、结构化输出或引用校验失败时，任务保留确定性结论并安全降级。Report Draft 与最终投影分别写入 `report_draft_vN.json` 和 `report_expression_vN.json` 供审计。

## 4. 环境要求

已验证环境：

- Windows 11；
- Python 3.11.15；
- LangGraph 1.2.11；
- langgraph-checkpoint 4.2.0；
- langgraph-checkpoint-sqlite 3.1.1；
- FastMCP 3.4.7；
- MCP 1.29.0；
- Chainlit 2.11.1；
- Plotly 6.9.0；
- Pydantic 2.13.4；
- pytest 9.1.1。

推荐使用 Conda：

```powershell
conda create -n env_agent python=3.11 pip -y
conda activate env_agent
python -m pip install -r requirements-dev.txt
python -m pip check
```

如果只运行应用，不执行测试和 Ruff：

```powershell
python -m pip install -r requirements.txt
```

## 5. 配置

复制示例文件：

```powershell
Copy-Item .env.example .env
```

模型连接字段：

```dotenv
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_NAME=
MODEL_API_KEY=
```

主链路 LLM 表达字段（M4-A 风险解释、M4-B 调查表达与 M4-C2 Report Draft 共用）：

```dotenv
# deterministic（默认）| llm
ANALYSIS_MODE=deterministic
# 留空时复用 MODEL_NAME
ANALYSIS_MODEL=
# true：先流式思考并生成公开过程摘要，再关闭思考生成严格 JSON
# ANALYSIS_LLM_ENABLE_THINKING=false
# 思考流首 Token/连续读取空闲边界，不是总运行时长
ANALYSIS_LLM_THINKING_TTFT_SECONDS=30
ANALYSIS_LLM_THINKING_BUDGET_TOKENS=800
ANALYSIS_LLM_PROCESS_SUMMARY_MAX_CHARS=400
ANALYSIS_LLM_TIMEOUT_SECONDS=60
ANALYSIS_LLM_MAX_RETRY=1
ANALYSIS_LLM_MAX_INPUT_CHARS=30000
ANALYSIS_LLM_MAX_OUTPUT_TOKENS=1200
# M4-B Evidence Summary / Query Proposal 独立输出上限
ANALYSIS_LLM_RESEARCH_MAX_OUTPUT_TOKENS=2400
```

启用 `ANALYSIS_MODE=llm` 前，必须在用户维护的 `.env` 中配置 `MODEL_API_KEY`，以及 `ANALYSIS_MODEL` 或 `MODEL_NAME`。DashScope Qwen 可设置 `ANALYSIS_LLM_ENABLE_THINKING=true` 启用两阶段协议：第一阶段流式消费思考并生成限长公开过程摘要，第二阶段显式关闭思考生成严格 JSON；原始 `reasoning_content` 不写入 Trace、Artifact 或报告。思考流默认 30 秒未收到有效 Token或连续 30 秒无数据时重试；结构化流使用 `ANALYSIS_LLM_TIMEOUT_SECONDS=60` 作为连续读取空闲边界。只要流数据持续到达，不设置总生成时长截止。网关关闭 OpenAI SDK 的隐式重试，只执行 `ANALYSIS_LLM_MAX_RETRY` 定义的应用级重试。M4-B 的 JSON 结构显著大于风险叙事和报告草稿，因此通过 `ANALYSIS_LLM_RESEARCH_MAX_OUTPUT_TOKENS` 使用独立的 2400-token 默认上限。默认 `deterministic` 不会调用模型，仍生成可审计的确定性风险解释 Artifact。

当 `FACT_VERIFIER=llm` 时，核验器属于独立的严格 JSON 阶段，默认关闭思考，不继承主链路的两阶段开关；只有显式设置 `FACT_VERIFIER_ENABLE_THINKING` 才会覆盖。核验请求携带严格 JSON Schema，SDK 隐式重试关闭，实际尝试次数由 `FACT_VERIFIER_MAX_RETRY` 控制。

运行配置：

```dotenv
CHECKPOINT_DB_PATH=checkpoints/credra_agent.db
MAX_RETRY=2
RESEARCH_FAIL_FIRST=0
DEBT_RATIO_THRESHOLD=0.15
CASHFLOW_THRESHOLD=0.0
REVENUE_THRESHOLD=0.20
DATA_DIR=data
TRACE_DIR=traces
```

`.env` 由本地用户维护且已被 Git 忽略。不要在日志、截图或提交中暴露 API Key。

## 6. CLI快速开始

正式 Durable Demo 使用 `app.task_cli`。

`parse` 只生成并持久化 TaskSpec，不发起模型决策或外部调查：

```powershell
python -m app.task_cli parse `
  --thread-id intent-demo-001 `
  --message-id intent-demo-001-message-1 `
  --as-of 2026-09-08 `
  --text "调查比亚迪2025年的现金质量，只使用交易所公告"
```

Chainlit 也接受同类自然语言并展示 TaskSpec 或待澄清项。`INTENT_MODE=llm` 可启用受约束的结构化模型解析；默认 `deterministic`，模型不可用时执行规则 fallback，并在结果中显示 `LLM_INTENT_FALLBACK`。

`agent` 把自然语言接入版本化 Runtime。agentic 和 shadow 均要求显式传入与当前 TaskSpec 版本绑定的 `RunAuthorization` JSON；缺少授权时返回 `AUTHORIZATION_REQUIRED`，不会调用模型或搜索服务：

```powershell
python -m app.task_cli agent `
  --thread-id agent-demo-001 `
  --message-id agent-demo-001-message-1 `
  --as-of 2026-09-11 `
  --execution-mode agentic `
  --authorization .\run-authorization.json `
  --text "调查比亚迪2025年的回款质量，只使用交易所公告"
```

`shadow` 记录通过策略校验的模型计划，但不执行模型选择的工具；实际业务结果由独立的 baseline 任务产生。真实模型或搜索试跑仍受 D05 单次预算授权约束，程序不会生成隐式批准或默认额度。

也可以使用封装好的演示脚本自动生成不冲突的 `thread_id`：

```powershell
.\scripts\demo.ps1 normal
.\scripts\demo.ps1 risky
.\scripts\demo.ps1 retry
```

### Case A：正常企业

```powershell
python -m app.task_cli start `
  --thread-id normal-demo-001 `
  --case-id case_normal
```

预期：

```text
status=COMPLETED
risk_level=LOW
interrupts=[]
```

报告位于任务独立目录；实际 `run_id` 可从命令返回的 `state.run_id` 查看：

```text
data/case_normal/runs/<run_id>/output/credit_report.md
```

### Case B：风险企业

```powershell
python -m app.task_cli start `
  --thread-id risky-demo-001 `
  --case-id case_risky
```

预期：

```text
status=WAITING_APPROVAL
next=["approval"]
interrupts 包含风险项、证据和允许的人工决策
```

查询持久化状态：

```powershell
python -m app.task_cli status --thread-id risky-demo-001
```

批准并生成报告：

```powershell
python -m app.task_cli resume `
  --thread-id risky-demo-001 `
  --decision approve `
  --comment "人工复核完成，继续生成报告。"
```

### 补充调查

```powershell
python -m app.task_cli resume `
  --thread-id risky-demo-001 `
  --decision research `
  --comment "需要补充调查。"
```

任务会生成：

```text
research_result_v2.json
risk_analysis_v2.json
investigation_intent_v2.json
query_plan_v2.json
```

并再次进入 `WAITING_APPROVAL`。旧的 v1 Artifact 不会被覆盖。

## 7. 跨进程Resume演示

1. 启动风险 Case：

   ```powershell
   python -m app.task_cli start --thread-id restart-demo-001 --case-id case_risky
   ```

2. 确认状态为 `WAITING_APPROVAL` 后，完全关闭当前终端或 Python 进程。

3. 打开新终端，激活环境：

   ```powershell
   conda activate env_agent
   ```

4. 不提供 `case_id`，只用原 `thread_id` 查询：

   ```powershell
   python -m app.task_cli status --thread-id restart-demo-001
   ```

5. 恢复：

   ```powershell
   python -m app.task_cli resume `
     --thread-id restart-demo-001 `
     --decision approve `
     --comment "跨进程恢复验证。"
   ```

6. 确认 `status=COMPLETED`，并检查报告中的人工审核意见。

## 8. Retry与故障注入

PowerShell 当前会话中开启 fail-first：

```powershell
$env:RESEARCH_FAIL_FIRST="1"
$env:MAX_RETRY="2"

python -m app.task_cli start `
  --thread-id retry-demo-001 `
  --case-id case_risky
```

预期两个 Research Tool 都发生：

```text
第一次调用 → TimeoutError → RETRY
第二次调用 → SUCCESS
```

查看 Trace：

```powershell
Get-Content traces/retry-demo-001.jsonl
```

恢复当前终端的默认故障设置：

```powershell
Remove-Item Env:RESEARCH_FAIL_FIRST
Remove-Item Env:MAX_RETRY
```

故障注入状态按 `task_id + tool_name` 隔离。重复演示时应使用新的 `thread_id`。

## 9. Chainlit UI

启动：

```powershell
chainlit run chainlit_app.py
```

浏览器访问 Chainlit 输出的本地地址，然后可以：

- 从首页预检列表直接启动 Case；
- 点击“恢复已有任务”并输入 `thread_id`；
- 查看 Case、Thread、Run、状态、当前/下一节点和风险等级；
- 查看结构化输入预检、节点进度和当前 Artifact 引用；
- 在内嵌 Agent 流程卡片中区分已完成、执行中、等待人工、跳过、失败和待执行节点，并查看整体完成比例；
- 在右侧任务清单中查看六节点状态，并在对话流中展开节点完成、工具调用、Retry、模型调用、Interrupt 和 Resume 的公开审计步骤；
- 在启动和 Resume 期间观察基于当前 Thread Trace 增量更新的实时节点进度，并在操作结束后由 SQLite Checkpoint 终态收敛；
- 查看五项既有财务指标的年度趋势，以及当前 Research/Risk Artifact 的 Evidence 来源等级、核验状态和风险类别统计；
- 风险任务使用“批准并继续”或“补充调查”按钮，并填写人工意见；
- 点击“刷新状态”读取 Durable Runtime 的最新状态。

`cases`、`start <case_id>`、`status <thread_id>` 与 `resume ...` 文本命令继续保留为兼容入口。M3-A 已完成工作台首页与状态总览；M3-B 已增加调查计划、Evidence、正文/Verifier 状态、Artifact 版本历史和 Retry/Trace 摘要；M3-C 已提供安全的 Markdown/HTML 报告预览和下载；M3-D1 已增加不复制业务状态的 Agent 流程总览；M3-D2 已增加 `TaskList` 与公开操作 `Step`；M3-D3 已增加业务指标图表；M3-D4 已增加启动和 Resume 期间的实时 Trace 桥接。M4-A/B 展示风险解释、Evidence Summary 与人工审核式 Query Proposal 的状态、引用和降级信息；任务完成后，最终报告及 Artifact 列表还会展示 M4-C2 Report Draft 与 M4-C1 二次校验审计结果。

Evidence 中只有通过安全检查的公开 `http/https` URL 会呈现为可点击链接；页面最多展示前 12 条 Evidence 和最近 16 个 Trace 事件，完整数据仍保留在当前 Run Artifact 与任务 Trace 中。工作台不读取或展示正文快照。

Chainlit 只调用 Durable Runtime，不独立维护任务状态。UI 重启后仍能凭 `thread_id` Resume。M3-D4 在启动或 Resume 时先显示受限实时进度，后台线程执行原同步 Runtime，页面约每 350 ms 检查一次当前 Thread 的追加式 Trace；右侧任务清单随 `NODE_START`、`NODE_END` 和 `INTERRUPT` 更新，公开 Step 按 Thread 去重并最多投影最近 12 条有业务意义的事件。操作返回后，实时投影必须由 SQLite Checkpoint Payload 覆盖；浏览器断开不会把页面临时状态写入业务状态。`TASK_START`、`NODE_START`、`TASK_STATE` 和 `STATUS_QUERY` 等高频噪声仍不生成公开 Step，进度消息也不展示 Trace 原始摘要、Chain of Thought、完整 Prompt、模型原始响应或正文。

M3-D3 图表只消费当前 Run 已存在的受限 Artifact 投影，不会发起模型、搜索或正文抓取调用。Financial Artifact 当前保存的是营收增长率、净利润率、经营现金流、流动比率和资产负债率，因此图表不把前两项误写成营收/利润绝对值；Research 或 Risk 缺失、损坏时会显示占位，并继续展示其他可用图表。

## 10. 运行产物

```text
data/<case_id>/
├── source/                 # 版本控制内的固定输入
└── runs/                   # 按任务隔离，运行生成且 Git 忽略
    └── <run_id>/
        ├── artifacts/
        │   ├── company_profile_v1.json
        │   ├── financial_analysis_v1.json
        │   ├── investigation_intent_v1.json
        │   ├── query_plan_v1.json
        │   ├── research_result_v1.json
        │   ├── evidence_summary_v1.json
        │   ├── query_proposal_v1.json
        │   ├── risk_analysis_v1.json
        │   ├── risk_narrative_v1.json
        │   ├── report_draft_v1.json
        │   └── report_expression_v1.json
        └── output/
            └── credit_report.md

checkpoints/
└── credra_agent.db         # SQLite任务状态，Git忽略

traces/
├── <thread_id>.jsonl       # 执行Trace，Git忽略
└── .fault_state/           # 故障注入状态，Git忽略
```

Graph State 仅保存 Artifact Reference 和路由字段，不保存完整财务数据、Research 结果或报告。
M2.2 之前创建且已写入 Case 根目录的旧 Checkpoint 会继续读取原目录；系统不会静默移动或覆盖既有用户数据。新任务一律使用 `runs/<run_id>/`。

## 11. 测试与验证

一键执行项目验证：

```powershell
.\scripts\verify.ps1
```

等价命令：

```powershell
python -m ruff check app spikes tests chainlit_app.py
python -m ruff format --check app spikes tests chainlit_app.py
python -m pytest -q
python -m pip check
python -m tests.stdio_research_smoke
```

覆盖范围包括：

- 财务公式和输入边界；
- Artifact 安全和版本；
- Case A/B 路由；
- MCP Tool 与独立 stdio Server；
- HITL 和跨进程 Resume；
- Retry、故障注入与 incomplete 披露；
- Investigation Intent、Query 差异和人工意见传递；
- Evidence-Risk 类别映射、证据缺口与冲突披露；
- 同 Case 多任务隔离及持久化 FAILED 状态；
- JSONL Trace Schema；
- 五个固定 Regression Cases；
- Chainlit 渲染边界；
- M4-A 风险解释、M4-B Evidence Summary / Query Proposal、M4-C2 Report Draft 与 M4-C1 报告投影的 Schema、引用边界、Unsupported Claim 拦截、降级和主链路回归。
- M5-A 固定业务 Eval 的人工财务基线、风险/HITL/报告检查、负向 Evidence 和基线漂移检测。
- M5-B 审计包的成员白名单、清单哈希、敏感信息/本地路径拦截、损坏输入和安全失败边界。

Chainlit 的传递依赖 `traceloop` 目前可能产生 Pydantic 旧式 Config 的弃用警告，不影响测试通过或项目代码。

### 固定业务 Eval

M5-A 通过正式 Durable Runtime 执行比亚迪与上汽集团两套公开 Case，并将财务、异常、风险、HITL、报告、来源兼容性和已固化的无关搜索结果与人工基线比较：

```powershell
python -m app.eval_cli run
```

默认 Suite 为 `evals/suites/auto_manufacturers_v1.json`，包含深交所比亚迪和上交所上汽集团，共 32 项检查；原单 Case `evals/suites/byd_baseline_v1.json` 仍可通过 `python -m app.eval_cli run --suite evals/suites/byd_baseline_v1.json` 独立运行。每次运行写入唯一的 `eval-results/<suite>-<execution>/eval_result.json`；该运行目录已被 Git 忽略。CLI 输出 `PASS/FAIL`、通过检查数和结果路径，业务检查失败时返回退出码 `1`，Manifest 或路径无效时返回 `2`。Eval 强制使用 `deterministic + mock + disabled content fetch + rules verifier`，不会读取 `.env` 文件，也不会调用 Tavily/Qwen；结果中的 `external_call_count` 固定为 `0`。

### 单次任务审计包

M5-B 可将一个已完成的新式 Durable Task 导出为独立 ZIP：

```powershell
python -m app.audit_cli export --thread-id <已完成的-thread-id>
```

默认输出到 Git 忽略的 `audit-exports/`，包含 Markdown/HTML 报告、来源清单、受限 Evidence 投影、版本化 Artifact、聚合 Trace、运行指标和带 SHA-256 的清单。导出不会执行搜索或模型调用，也不会写入 `.env`。运行模式只根据历史 Run Artifact 推导，不使用导出时的当前配置冒充历史配置；现有 Artifact 无法证明的正文抓取 Provider 明确标为 `NOT_RECORDED`。Checkpoint、原始 Trace、搜索/正文/核验 Snapshot 和故障状态不会入包；未完成或旧式任务、危险 Thread ID、损坏 Artifact、凭据或绝对本地路径会被拒绝。同名 ZIP 不覆盖，重复导出时应先人工处理原文件或指定新的 `--output-dir`。

## 12. 入口说明

| 入口 | 用途 |
|---|---|
| `python -m app.task_cli` | 正式 Durable CLI，推荐 |
| `python -m app.eval_cli run` | 固定离线业务 Eval |
| `python -m app.audit_cli export` | 已完成任务的审计 ZIP 导出 |
| `chainlit run chainlit_app.py` | 最小审核 UI |
| `python -m app.mcp.research_server` | 手工启动 MCP Server，通常无需使用 |
| `python -m app.main <case_dir>` | 无 Checkpoint 的快速离线预览 |
| `python -m spikes.durable_execution.cli` | Day 0 Runtime Spike |

## 13. 常见问题

### `thread already exists`

一个 `thread_id` 对应一条持久任务。使用新 ID，或继续对原 ID 执行 `status/resume`。

### `thread is not waiting for approval`

任务可能已完成或尚未到达 interrupt。先执行 `status` 查看 `state.status` 和 `interrupts`。

### MCP显示 `Connection closed`

确认在仓库根目录运行、已激活正确 Python 环境，并验证：

```powershell
python -m tests.stdio_research_smoke
```

### Chainlit无法导入`app`

请从仓库根目录使用根入口：

```powershell
chainlit run chainlit_app.py
```

不要直接运行 `chainlit run app/chainlit_app.py`。

### Retry演示没有再次失败

故障状态按任务隔离。同一个 `thread_id` 的首次故障已经消费后不会重复注入，请使用新的 `thread_id`。

### Pytest临时目录提示`WinError 5`

统一使用 `scripts/verify.ps1`。脚本会在仓库的 `.test-tmp/` 下为本次运行创建独立临时目录，并禁用 pytest 缓存，避免访问损坏或权限异常的用户临时目录和 `.pytest_cache`；本次目录会在验证结束后自动清理。

## 14. 项目边界

MVP 不实现：

- 自动贷款批准/拒绝或授信额度；
- 银行级风控模型；
- 实时金融数据库；
- FastAPI、PostgreSQL、Redis、Kubernetes；
- A2A、长期记忆、完整 Agent Eval；
- PDF/OCR/VLM 与企业级 Sandbox。

## 15. 面试讨论要点

- 为什么已知财务异常使用规则路由，而不是 LLM Routing？
- 为什么财务计算必须由 Python Tool 完成？
- 为什么任务生命周期需要长于进程和 UI Session？
- 为什么 interrupt 前不能执行不可幂等副作用？
- 为什么 State 只保存 Artifact Reference？
- 为什么只将 Research Tool MCP 化？
- Retry 耗尽后为什么不能静默忽略？
- 如何证明 Resume 没有重新执行已完成节点？

完整演示命令见 [演示指南](docs/演示指南.md)，项目表述边界与问题参考见 [项目展示与面试说明](docs/项目展示与面试说明.md)。详细需求、技术设计、开发计划和适应性变更记录均位于 [docs](docs/) 目录。

## 15. M1 Case 导入前校验

使用 `app.case_cli` 创建、校验和启动结构化 Case。它不会修改 `.env`；`run` 会先完成导入前校验，失败时不会创建任务 Checkpoint。

```powershell
# 创建可编辑模板（会创建 data/case_my_company/source/）
python -m app.case_cli init --case-id case_my_company

# 校验来源、企业名称、年度、单位、财务勾稽及人工确认项
python -m app.case_cli validate --case-id case_byd_002594

# 校验通过后启动 Durable Runtime
python -m app.case_cli run `
  --case-id case_byd_002594 `
  --thread-id byd-m1-demo-001
```

新 Case 使用 `source_manifest_v2`，要求每个关键财务字段记录来源、页码、表格、原始单位、内部单位 `CNY_1000` 和会计口径。`CNY`、`CNY_10K`、`CNY_1000` 和 `CNY_100M` 在预检中会换算为 `CNY_1000`；原始源文件保持不变，以便与公开年报逐项核对。旧 `case_normal` 和 `case_risky` 仍可运行，但校验会提示缺少来源清单的迁移警告。
