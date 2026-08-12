# Credra Agent MVP——企业授信尽调长任务智能体需求分析与技术设计方案

**版本：** v1.0  
**项目类型：** 个人技术项目 / Agent Engineering Demo  
**目标开发周期：** 4–6 个有效开发日  
**目标工作量：** 约 29–38 小时  
**主要目标岗位：** AI Solution Architect / Agent Solution Architect / AI Application Engineer

---

# 1. 项目定位

Credra Agent 是一个以**企业授信尽调**为业务载体的长任务 Agent Demo。

项目并不试图实现完整银行授信或风控系统，而是通过一个具有明确业务流程、计算任务、外部调查和人工审核需求的场景，验证较接近生产环境的 Agent Runtime 能力：

> **Dynamic Execution + Checkpoint / Resume + Human-in-the-loop + Tool Execution + Artifact Management + MCP + Trace / Retry**

项目核心价值不是证明：

> “能够做一个金融 Agent。”

而是证明：

> **能够围绕真实业务场景设计并实现一个具备状态管理、动态执行、工具调用、人工介入、上下文治理和基础故障恢复能力的长任务 Agent 系统。**

---

# 2. 设计原则

整个 MVP 遵循以下原则。

## 2.1 Reliability First

优先实现：

- Checkpoint；
- Resume；
- HITL；
- 确定性 Routing；
- Artifact；
- Retry。

而不是优先增加 Agent 数量。

---

## 2.2 Deterministic Where Possible

对于可以用程序可靠解决的问题，不交给 LLM 自由判断。

例如：

```text
财务计算         → Python
异常指标判断     → Rule
Graph Routing    → Rule
结构约束         → Pydantic
风险解释         → LLM
报告表达         → LLM
```

---

## 2.3 LLM for Reasoning, Code for Execution

核心原则：

> **LLM 负责理解、解释和语义决策；程序负责计算、状态和执行控制。**

---

## 2.4 MVP Before Platform

首版不建设：

- 完整 Agent Platform；
- A2A；
- Kubernetes；
- 企业级 Sandbox；
- 完整 Observability；
- 完整 Agent Eval；
- 复杂 Web UI。

项目首先保证：

> **主链路真实、稳定、可复现。**

---

# 3. 业务背景

企业授信尽调通常涉及：

- 企业基本资料；
- 财务报表；
- 经营信息；
- 行业信息；
- 外部风险信息；
- 风险分析；
- 人工审核。

任务天然具有：

```text
多步骤
+
多数据源
+
确定性计算
+
动态调查
+
人工审核
+
长生命周期
```

因此非常适合验证 Long-running Agent 的设计。

---

# 4. MVP业务目标

用户选择一个模拟企业 Case 后，系统完成：

```text
企业资料
    ↓
信息结构化
    ↓
财务分析
    ↓
异常检测
    ↓
是否需要外部调查？
   ↙           ↘
 Yes           No
 ↓              │
Research         │
   ↘            ↙
       风险分析
           ↓
       人工审核
           ↓
       尽调报告
```

---

# 5. 非目标

MVP 明确不实现：

- 自动批准/拒绝贷款；
- 自动生成授信额度；
- 银行级风控模型；
- 模型训练或 SFT；
- OCR 平台；
- 金融实时数据库；
- 多租户；
- RBAC / IAM；
- A2A；
- Long-term Memory；
- Redis；
- PostgreSQL；
- Kubernetes；
- 多模型自动 Fallback；
- 完整 Sandbox 安全系统；
- 完整 Agent Evaluation Platform。

---

# 6. Demo Case

MVP 固定维护两个 Case。

---

## 6.1 Case A —— 正常企业

主要特点：

- 营收稳定增长；
- 利润稳定；
- 负债率正常；
- 经营现金流健康。

预期路径：

```text
Document
    ↓
Financial
    ↓
Risk
    ↓
Human Review
    ↓
Report
```

---

## 6.2 Case B —— 风险企业

主要特点：

- 营收快速增长；
- 经营现金流下降；
- 负债率持续提升。

Financial Agent 产生：

```text
REVENUE_CASHFLOW_DIVERGENCE
DEBT_RATIO_RISING
```

预期路径：

```text
Document
    ↓
Financial
    ↓
Anomaly
    ↓
Research
    ↓
Risk
    ↓
Human Review
    ↓
Report
```

两个 Case 的核心作用：

> **证明 Graph 会根据运行时 State 改变执行路径。**

---

# 7. 系统架构

```text
                    User
                      │
                      ▼
                  Chainlit
                      │
                      ▼
               Credra Agent Service
                      │
                      ▼
               LangGraph Runtime
                      │
                      ▼
                 Coordinator
                      │
            Runtime Conditional Routing
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
     Document      Financial    Research
      Agent          Agent       Agent
                       │            │
                       ▼            ▼
                 Python Tool       MCP
         └────────────┼────────────┘
                      ▼
                  Risk Agent
                      │
                      ▼
                 interrupt()
                      │
                 Human Review
                      │
                      ▼
                 Final Report
```

底层 Runtime：

```text
┌─────────────────────────────────────┐
│            Agent Runtime            │
│                                     │
│ SQLite Checkpoint                   │
│ Artifact Store                      │
│ Retry                               │
│ JSONL Trace                         │
│ Task / Thread State                 │
└─────────────────────────────────────┘
```

---

# 8. 为什么不使用 FastAPI

MVP 阶段不增加 FastAPI。

调用链直接采用：

```text
Chainlit
   ↓
Application Service
   ↓
LangGraph
```

原因：

- 没有外部调用方；
- 不需要多端 API；
- Chainlit 本身即可承载 Demo；
- 减少状态同步复杂度；
- 节省开发和调试时间。

V2 如果需要将 Agent Runtime 服务化，再增加 FastAPI。

---

# 9. Agent角色

系统只保留四个业务 Agent。

不额外设计 Planner / Reviewer / Supervisor 等大量角色。

---

# 10. Document Agent

## 职责

负责将输入企业信息转换为统一结构。

输入：

```text
company_profile.json
business_info.md
financial_statement.json
```

输出：

```json
{
  "company_name": "",
  "industry": "",
  "registered_capital": "",
  "established_date": "",
  "shareholders": [],
  "business_scope": "",
  "major_customers": [],
  "major_suppliers": []
}
```

输出保存为 Artifact：

```text
company_profile.json
```

---

# 11. Financial Agent

Financial Agent 负责：

1. 获取财务数据；
2. 调用 Financial Tool；
3. 分析计算结果；
4. 生成 anomaly flags；
5. 输出财务分析 Artifact。

---

## 11.1 财务指标

MVP 仅计算：

- Revenue Growth；
- Net Profit Margin；
- Current Ratio；
- Debt Ratio；
- Operating Cash Flow Trend。

---

## 11.2 Tool输出

统一返回：

```json
{
  "metric": "debt_ratio",
  "values": [
    0.41,
    0.48,
    0.63
  ],
  "unit": "ratio",
  "formula": "total_liabilities / total_assets"
}
```

保留：

- 指标值；
- 单位；
- 计算公式。

便于：

- Report；
- Trace；
- 审计；
- Debug。

---

# 12. Anomaly Detection

异常检测不由 LLM 自由判断。

使用代码规则。

例如：

```python
if revenue_growth > threshold_1 and cashflow_growth < threshold_2:
    anomaly_flags.append(
        "REVENUE_CASHFLOW_DIVERGENCE"
    )
```

以及：

```python
if debt_ratio[-1] - debt_ratio[0] > threshold:
    anomaly_flags.append(
        "DEBT_RATIO_RISING"
    )
```

阈值全部配置化。

例如：

```text
config.py
```

---

# 13. Coordinator设计

MVP 中 Coordinator 不设计成自由规划型 Agent。

主要依据：

```text
current_state
anomaly_flags
human_decision
```

进行 Graph Routing。

---

## 13.1 路由逻辑

```python
if state.anomaly_flags:
    return "research"

return "risk"
```

HITL：

```python
if state.human_decision == "research":
    return "research"

if state.human_decision == "approve":
    return "report"
```

---

## 13.2 为什么这样设计

相比 LLM Routing：

### 优点

- Demo稳定；
- 可回归测试；
- Token成本低；
- 更容易解释；
- Case A/B 可100%复现。

仍然具备 Dynamic Execution：

> 执行路径由 runtime State 决定，而非预先固定所有节点。

---

# 14. Research Agent

当 Financial Agent 发现 anomaly 时触发。

主要职责：

- 查询企业补充信息；
- 查询行业信息；
- 返回相关事实。

MVP Tools：

```text
search_company
search_industry
```

---

# 15. MCP设计

MVP 只 MCP 化 Research Tools。

架构：

```text
Research Agent
      ↓
MCP Client
      ↓
Research MCP Server
      ↓
Mock Dataset / Search Adapter
```

Server：

```text
Research MCP

├── search_company()
└── search_industry()
```

MVP 不建设：

- MCP Gateway；
- MCP Registry；
- 动态服务发现；
- 大规模 Tool Search。

---

# 16. Risk Agent

Risk Agent 根据：

- company profile；
- financial analysis；
- anomaly flags；
- research result；

生成风险分析。

输出使用 Pydantic 约束。

---

## 16.1 RiskLevel

```python
class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
```

---

## 16.2 Risk输出

```json
{
  "risk_level": "MEDIUM",
  "risk_flags": [
    {
      "type": "cashflow",
      "severity": "MEDIUM",
      "description": "经营现金流与收入增长趋势背离",
      "evidence": [
        "financial_analysis_v1"
      ]
    }
  ],
  "requires_human_review": true
}
```

---

# 17. HITL设计

当：

```text
risk_level = MEDIUM
```

或：

```text
risk_level = HIGH
```

进入：

```text
WAITING_APPROVAL
```

---

## 17.1 HumanDecision

不使用单一：

```text
human_feedback
```

改为：

```python
class HumanDecision(str, Enum):
    APPROVE = "approve"
    RESEARCH = "research"
```

State：

```text
human_decision
human_comment
```

---

## 17.2 用户界面

```text
发现以下风险：

1. 经营现金流持续下降
2. 资产负债率持续上升

[继续生成报告]

[补充调查]
```

---

# 18. Durable Execution

这是项目的最高优先级技术能力。

使用：

```text
LangGraph Checkpoint
+
SQLite
+
thread_id
```

保存任务状态。

---

## 18.1 Task生命周期

```text
CREATED
   ↓
RUNNING
   ↓
WAITING_APPROVAL
   ↓
RUNNING
   ↓
COMPLETED
```

异常时：

```text
FAILED
```

---

# 19. Resume Demo

项目必须支持以下演示：

```text
Start Case B
    ↓
Document
    ↓
Financial
    ↓
Research
    ↓
Risk
    ↓
interrupt
    ↓
WAITING_APPROVAL
```

此时：

> 完全关闭 Python 程序。

随后重新启动：

```text
resume(thread_id)
```

恢复：

```text
WAITING_APPROVAL
    ↓
Human Decision
    ↓
Report
```

这是 MVP 最重要的 Demo。

---

# 20. Interrupt节点规范

因为恢复后包含 interrupt 的 Node 可能重新执行，因此：

> **interrupt 之前不得产生不可重复的副作用。**

例如禁止：

```text
发送真实外部请求
写入重复业务记录
创建不可幂等资源
```

Node 必须尽量：

```text
读取State
→ 生成审批内容
→ interrupt
```

副作用放在 Resume 之后执行。

---

# 21. Day 0技术Spike

正式开发前必须完成。

预算：

> **2–3小时**

只实现：

```text
node_a
  ↓
interrupt()
  ↓
退出进程
  ↓
重新启动
  ↓
Command(resume)
  ↓
node_b
```

不接入：

- Agent；
- LLM；
- MCP；
- 金融逻辑；
- Chainlit业务页面。

验收标准：

- SQLite 中存在 checkpoint；
- Python 进程完全退出；
- 相同 thread_id 可以 Resume；
- Resume 后不重新执行已完成节点。

---

# 22. UI降级策略

Day 0 / Day 4 接入 Chainlit。

如果：

> Chainlit + HITL 联调超过约 2 小时仍未稳定，

立即降级为：

```text
CLI / 简单页面
+
手动输入 task_id
```

保证：

> Durable Execution 本身必须完成。

UI 流畅度不作为阻塞项。

---

# 23. Artifact Store

所有大块数据放入 Artifact。

目录：

```text
data/
└── case_risky/
    ├── source/
    │   ├── company_profile.json
    │   ├── financial_statement.json
    │   └── business_info.md
    │
    ├── artifacts/
    │   ├── company_profile_v1.json
    │   ├── financial_analysis_v1.json
    │   ├── research_result_v1.json
    │   └── risk_analysis_v1.json
    │
    └── output/
        └── credit_report.md
```

---

# 24. Artifact版本

Artifact 使用：

```text
name_v1
name_v2
```

避免 Resume 或重新 Research 时覆盖历史结果。

State 保存：

```json
{
  "financial_artifact":
    "financial_analysis_v1"
}
```

---

# 25. Context策略

Graph State 中不保存：

- 完整金融数据；
- 完整Research结果；
- 完整Report；
- 全量Conversation。

只保存：

```text
Artifact Reference
+
当前路由必要字段
+
anomaly_flags
+
human_decision
```

典型 State：

```text
task_id
case_id
status
current_node

company_artifact
financial_artifact
research_artifact
risk_artifact

anomaly_flags

risk_level

human_decision
human_comment
```

---

# 26. Retry

MVP 只实现 Research Tool Retry。

配置：

```text
MAX_RETRY=2
```

执行：

```text
search_company
     ↓
Timeout
     ↓
Retry
     ↓
Success
```

如果全部失败：

```text
external_research_incomplete = true
```

而不是静默忽略。

---

# 27. Fault Injection

不使用随机故障。

使用：

```bash
RESEARCH_FAIL_FIRST=1
```

第一次调用：

```text
raise TimeoutError
```

第二次：

```text
success
```

确保 Demo 每次行为一致。

---

# 28. Trace

MVP 使用：

```text
JSONL
```

不接 OpenTelemetry / Jaeger。

---

## 28.1 Trace结构

```python
class TraceEvent(BaseModel):

    task_id: str
    node: str
    event_type: str

    input_summary: str | None
    output_summary: str | None

    start_time: datetime
    end_time: datetime
    latency_ms: int

    status: Literal[
        "SUCCESS",
        "FAILED",
        "RETRY"
    ]

    error: str | None
```

---

# 29. Prompts

所有 Prompt 外置。

```text
app/prompts/

├── document.md
├── financial.md
├── research.md
├── risk.md
└── report.md
```

避免写成：

```python
SYSTEM_PROMPT = """
...
"""
```

方便：

- Prompt迭代；
- Git Diff；
- 面试展示；
- 单独测试。

---

# 30. Final Report

Report 使用固定 Template + LLM 填充。

章节：

```text
企业授信尽调分析报告

1. 企业概况
2. 财务情况
3. 财务趋势
4. 外部经营调查
5. 风险项
6. 风险证据
7. 人工审核意见
8. 综合分析
```

Prompt 必须明确：

> 不生成最终贷款批准/拒绝决策。

---

# 31. 配置管理

新增：

```text
app/config.py
.env
.env.example
```

配置项包括：

```text
MODEL_NAME
MODEL_API_KEY

CHECKPOINT_DB_PATH

MAX_RETRY

RESEARCH_FAIL_FIRST

DEBT_RATIO_THRESHOLD
CASHFLOW_THRESHOLD
REVENUE_THRESHOLD

DATA_DIR
TRACE_DIR
```

---

# 32. 依赖管理

所有关键 Agent Framework 依赖应：

> **在 Day 0 完成 Spike 后锁定实际验证过的版本。**

尤其：

```text
langgraph
langchain-core
langgraph checkpoint sqlite package
chainlit
mcp / fastmcp
pydantic
```

原则：

> 不在开发过程中自动升级核心框架。

---

# 33. 最新项目目录

```text
Credra Agent/
│
├── app/
│   │
│   ├── agents/
│   │   ├── document.py
│   │   ├── financial.py
│   │   ├── research.py
│   │   └── risk.py
│   │
│   ├── graph/
│   │   ├── state.py
│   │   ├── routing.py
│   │   └── workflow.py
│   │
│   ├── models/
│   │   ├── company.py
│   │   ├── financial.py
│   │   ├── risk.py
│   │   └── trace.py
│   │
│   ├── prompts/
│   │   ├── document.md
│   │   ├── financial.md
│   │   ├── research.md
│   │   ├── risk.md
│   │   └── report.md
│   │
│   ├── tools/
│   │   ├── financial.py
│   │   └── artifacts.py
│   │
│   ├── mcp/
│   │   └── research_server.py
│   │
│   ├── runtime/
│   │   ├── tracing.py
│   │   └── fault.py
│   │
│   ├── config.py
│   └── main.py
│
├── data/
│   ├── case_normal/
│   └── case_risky/
│
├── checkpoints/
│   └── credra_agent.db
│
├── traces/
│
├── tests/
│   ├── test_financial.py
│   ├── test_routing.py
│   └── test_resume.py
│
├── .env.example
├── requirements.txt
└── README.md
```

---

# 34. 最小测试集

MVP 不建设完整 Eval。

只维护五个 Regression Case：

```text
normal_01
normal_02
risk_cashflow
risk_debt
tool_failure
```

---

## 34.1 Routing测试

正常企业：

```python
assert execution_path == [
    "document",
    "financial",
    "risk"
]
```

异常企业：

```python
assert execution_path == [
    "document",
    "financial",
    "research",
    "risk"
]
```

---

## 34.2 Retry测试

```python
assert retry_count == 1
assert tool_status == "SUCCESS"
```

---

## 34.3 Resume测试

```text
Start
→ Interrupt
→ Kill
→ Restart
→ Resume
→ Completed
```

该测试优先级高于普通 Agent 单元测试。

---

# 35. 开发计划

## Day 0 —— Technical Spike

**2–3h**

完成：

```text
Checkpoint
Interrupt
Kill Process
Resume
```

结果：

> 决定 LangGraph + SQLite Durable Execution 是否进入主方案。

---

# 36. Day 1 —— Core Business Flow

**5–6h**

完成：

- Config；
- Schema；
- Case数据；
- Financial Tool；
- Financial Agent；
- Risk Agent；
- Report Prompt。

验收：

```text
Financial
→ Risk
→ Report
```

---

# 37. Day 2 —— Artifact + Routing

**5–6h**

完成：

- Document Agent；
- Artifact Store；
- Artifact Version；
- Anomaly Flags；
- Rule-based Routing；
- Case A / Case B。

验收：

两个 Case 走不同路径。

---

# 38. Day 3 —— Research + MCP

**4–5h**

完成：

- Research Agent；
- Research MCP Server；
- search_company；
- search_industry；
- Research Artifact。

验收：

Case B 自动进入 Research。

---

# 39. Day 4 —— HITL + Full Resume

**5–7h**

完成：

- Chainlit最小UI；
- Risk展示；
- interrupt；
- HumanDecision；
- Resume；
- Restart Demo。

如果 UI 集成受阻：

> 立即降级 CLI / Task ID。

---

# 40. Day 5 —— Retry + Trace + Regression

**4–6h**

完成：

- Fault Injection；
- Retry；
- JSONL Trace；
- 五个 Regression Cases；
- test_resume。

---

# 41. Day 6 —— Showcase

**4–5h**

完成：

- README；
- Mermaid架构图；
- Demo脚本；
- 代码清理；
- 简历描述；
- 面试问题整理。

---

# 42. 总工作量

预计：

> **29–38小时**

实际开发目标：

> **5–6个有效开发日。**

如果时间只有4天：

优先舍弃：

```text
Chainlit UI美化
MCP故障恢复
Artifact版本管理
部分Regression Test
```

绝不能舍弃：

```text
Checkpoint
Resume
HITL
Dynamic Routing
Python Tool
Artifact
```

---

# 43. MVP验收标准

## P0 —— 必须

- [ ] Case A完整运行
- [ ] Case B完整运行
- [ ] 两个Case执行路径不同
- [ ] 财务指标由Python计算
- [ ] Anomaly Flags由规则生成
- [ ] 中间结果使用Artifact
- [ ] Risk Level使用Pydantic Enum
- [ ] HITL可以暂停
- [ ] Checkpoint写入SQLite
- [ ] 完全重启程序后可以Resume
- [ ] 最终生成报告

---

## P1 —— 强烈建议

- [ ] Research通过MCP调用
- [ ] Research Timeout可Retry
- [ ] Fault Injection可控
- [ ] Trace写入JSONL
- [ ] 5个Regression Cases
- [ ] Prompt全部外置

---

## P2 —— 时间允许

- [ ] Chainlit完整UI
- [ ] Artifact Versioning
- [ ] Makefile
- [ ] Demo录屏

---

# 44. 明确延期项

全部移入 V2：

```text
Agent Eval
A2A
PDF / VLM
Docker Sandbox
Context Compression
Token Budget
Model Fallback
OpenTelemetry
PostgreSQL
Redis
FastAPI
Kubernetes
```

---

# 45. V2扩展

## Agent Eval

增加：

- Task Success Rate；
- Routing Accuracy；
- Tool Success Rate；
- Recovery Rate；
- Average Steps；
- Token Cost；
- Human Intervention Rate。

---

## Fault Recovery

增加：

```text
LLM 429
Invalid JSON
MCP Timeout
Model Timeout
Tool Failure
```

并支持：

```text
Retry
Fallback
Human Escalation
```

---

## Context Engineering

增加：

- Context Budget；
- Artifact Retrieval；
- Context Compression；
- Working Memory。

---

## Multimodal

增加：

```text
PDF
 ↓
Parser
 ↓
VLM Fallback
 ↓
Structured Artifact
```

---

## Sandbox

增加 Docker：

```text
Agent
 ↓
Docker Sandbox
 ↓
Python Execution
```

---

## A2A

当某个 Agent 真正成为独立服务后：

```text
Main Runtime
     │
     │ A2A
     ▼
Remote Research Agent
```

而不是为了技术关键词在单体系统内部加入 A2A。

---

# 46. 项目最终简历表达

MVP 完成后建议只写两条：

```latex
\item 面向企业授信尽调场景构建长任务Agent系统，根据财务异常与案件状态动态委派财务分析、外部调查及风险分析等执行节点；基于Checkpoint与Human-in-the-loop支持任务暂停、程序重启后的跨Session恢复及风险结论人工审核。

\item 通过Python Tool完成财务指标计算与异常检测，并以Artifact Store隔离原始资料和中间结果；基于MCP接入外部调查工具，支持工具超时重试与JSONL执行Trace，降低长任务上下文与执行状态耦合。
```

完成 V2 Eval 后，再增加：

```latex
\item 构建覆盖Task Success、Routing Accuracy、Recovery Rate、Token Cost与Human Intervention的Agent评测体系，并通过故障注入验证长任务执行可靠性。
```

---

# 47. 核心面试故事

这个项目最终应该能够清楚回答：

### 为什么不是普通 Workflow？

因为异常案件需要额外调查，执行路径取决于运行时 State。

### 为什么 Routing 不全部交给 LLM？

MVP 中已知异常条件适合确定性规则，以提高稳定性、可测试性和成本效率。

### 为什么需要 Checkpoint？

Agent Task 生命周期可能长于进程或 HTTP Session 生命周期。

### 为什么需要 HITL？

高风险金融分析不应该直接由 Agent 自动形成最终决策。

### 为什么 Artifact 独立保存？

避免文件、中间结果和历史信息无限进入 Context Window。

### 为什么财务计算不用 LLM？

确定性计算应该交由程序执行，从而提高准确性和可复现性。

### 为什么只做一个 MCP Server？

MVP 用协议验证 Agent 与外部服务解耦即可，不需要为了 MCP 构建额外平台。

### 为什么没有 A2A？

Agent 仍属于同一个 Runtime 时，内部节点调度成本更低；真正拆成独立 Agent 服务后再引入跨 Agent 协议。

---

# 48. 最终项目价值

Credra Agen MVP 需要形成三层能力证明：

```text
业务层
企业授信尽调 / 风险辅助分析

        ↓

Agent层
Dynamic Execution
HITL
MCP
Tool Use

        ↓

Runtime层
Checkpoint
Resume
Artifact
Retry
Trace
```

项目最终重点不是：

> Agent 数量多。

也不是：

> 金融知识复杂。

而是：

> **用一个足够真实的金融场景，把长任务 Agent 从“能执行”推进到“能暂停、能恢复、能人工介入、能调用确定性工具、能追踪执行状态”。**

对于 MVP 而言，只要这一条技术主线完整跑通，即达到开发目标。