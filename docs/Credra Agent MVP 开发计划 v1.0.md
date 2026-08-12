# Credra Agent MVP 开发计划

**版本：** v1.0  
**制定日期：** 2026-08-12  
**项目名称：** Credra Agent  
**计划周期：** 5–6 个有效开发日  
**预计工作量：** 29–38 小时  
**依据文档：**《Credra Agent MVP——企业授信尽调长任务智能体需求分析与技术设计方案 v1.0》

---

# 1. 计划目标

本计划用于将需求方案转化为可执行、可验收的 MVP 开发任务。

Credra Agent 的交付重点不是构建完整授信系统，而是使用企业授信尽调场景验证以下长任务 Agent 能力：

```text
Dynamic Execution
+
Checkpoint / Resume
+
Human-in-the-loop
+
Deterministic Tool Execution
+
Artifact Management
+
MCP
+
Retry / Trace
```

最终系统必须能够稳定演示：

1. 正常 Case 与风险 Case 根据运行时状态走不同路径；
2. 财务计算和异常检测可复现、可测试；
3. 风险任务进入人工审核后能够暂停；
4. Python 进程完全退出后，可使用相同 `thread_id` 恢复；
5. 恢复后继续执行而不重复已完成节点；
6. 最终生成不包含自动授信决策的尽调分析报告。

---

# 2. 统一命名约定

后续代码、配置、文档和演示统一使用以下名称：

| 项目元素 | 统一名称 |
|---|---|
| 产品名称 | Credra Agent |
| Python 包/模块语义名 | `credra_agent` |
| Checkpoint 数据库 | `credra_agent.db` |
| 默认应用服务名 | `Credra Agent Service` |
| 报告标题 | 企业授信尽调分析报告 |

---

# 3. 开发原则

## 3.1 可靠性优先

实现优先级为：

```text
Checkpoint / Resume
    ↓
HITL
    ↓
Dynamic Routing
    ↓
Python Tool / Artifact
    ↓
MCP / Retry / Trace
    ↓
UI体验
```

任何 UI、Prompt 或展示层优化都不能阻塞 Durable Execution 主链路。

## 3.2 确定性优先

| 任务 | 实现方式 |
|---|---|
| 财务指标计算 | Python |
| 异常检测 | 配置化规则 |
| Graph Routing | 确定性函数 |
| 数据结构约束 | Pydantic |
| 风险解释 | LLM |
| 报告表达 | 固定模板 + LLM |

## 3.3 小步集成

每个阶段都必须形成可以独立运行和验收的纵向切片。不得在 Checkpoint Spike 未通过前一次性铺开全部业务模块。

## 3.4 状态与数据分离

Graph State 只保存路由和恢复所需的小字段；原始资料、大块分析结果及报告存放在 Artifact Store 中。

---

# 4. MVP范围

## 4.1 P0：必须交付

- Case A（正常企业）完整运行；
- Case B（风险企业）完整运行；
- 两个 Case 产生不同执行路径；
- Python 财务指标计算；
- 规则化异常检测；
- Artifact 持久化；
- Pydantic 风险结构；
- HITL 暂停和恢复；
- SQLite Checkpoint；
- 跨进程 Resume；
- 最终 Markdown 报告。

## 4.2 P1：强烈建议交付

- Research MCP Server；
- Research Tool Retry；
- 可控 Fault Injection；
- JSONL Trace；
- 五个 Regression Cases；
- Prompt 外置。

## 4.3 P2：时间允许

- 完整 Chainlit UI；
- Artifact 自动版本递增；
- Makefile 或统一任务脚本；
- Demo 录屏及扩展展示材料。

## 4.4 不在本期实现

- 自动贷款批准、拒绝或额度决策；
- FastAPI、PostgreSQL、Redis、Kubernetes；
- A2A、长期记忆、完整 Agent Eval；
- PDF/OCR/VLM；
- 企业级 Sandbox、RBAC、完整 Observability；
- 多模型自动 Fallback。

---

# 5. 目标目录结构

```text
Credra-Agent/
├── app/
│   ├── agents/
│   │   ├── document.py
│   │   ├── financial.py
│   │   ├── research.py
│   │   └── risk.py
│   ├── graph/
│   │   ├── state.py
│   │   ├── routing.py
│   │   └── workflow.py
│   ├── models/
│   │   ├── company.py
│   │   ├── financial.py
│   │   ├── risk.py
│   │   └── trace.py
│   ├── prompts/
│   │   ├── document.md
│   │   ├── financial.md
│   │   ├── research.md
│   │   ├── risk.md
│   │   └── report.md
│   ├── tools/
│   │   ├── financial.py
│   │   └── artifacts.py
│   ├── mcp/
│   │   └── research_server.py
│   ├── runtime/
│   │   ├── tracing.py
│   │   └── fault.py
│   ├── config.py
│   └── main.py
├── data/
│   ├── case_normal/
│   └── case_risky/
├── checkpoints/
│   └── credra_agent.db
├── traces/
├── tests/
├── docs/
├── .env.example
├── requirements.txt
└── README.md
```

目录在开发阶段按需创建，不提前生成无内容的占位模块。

---

# 6. 阶段计划总览

| 阶段 | 预计时间 | 核心产出 | 阶段门禁 |
|---|---:|---|---|
| Day 0：Durable Execution Spike | 2–3h | 最小暂停、退出、恢复验证 | 跨进程 Resume 成功 |
| Day 1：业务核心链路 | 5–6h | 财务、风险、报告基础链路 | Financial → Risk → Report 可运行 |
| Day 2：Artifact 与动态路由 | 5–6h | Document、Artifact、Case A/B | 两个 Case 路径不同 |
| Day 3：Research 与 MCP | 4–5h | MCP Server、Research Agent | 风险 Case 自动调查 |
| Day 4：HITL 与完整恢复 | 5–7h | 审核、暂停、跨会话恢复 | 重启后完成报告 |
| Day 5：可靠性与回归 | 4–6h | Retry、Trace、回归测试 | 核心测试全部通过 |
| Day 6：展示与收尾 | 4–5h | README、架构图、Demo 脚本 | 可重复演示和交接 |

---

# 7. Day 0：Durable Execution Technical Spike

## 7.1 目标

在引入业务逻辑前验证 LangGraph + SQLite 的核心持久化能力，消除项目最高技术风险。

最小流程：

```text
node_a
  ↓
interrupt()
  ↓
完全退出 Python 进程
  ↓
重新启动
  ↓
Command(resume=...)
  ↓
node_b
```

## 7.2 任务

- 建立最小 Python 运行环境；
- 选择并安装 LangGraph、SQLite Checkpointer 相关依赖；
- 实现仅包含两个节点的 Spike；
- 使用固定 `thread_id` 启动任务；
- 在 interrupt 后确认 Checkpoint 已写入 SQLite；
- 完全关闭进程；
- 在新进程中用同一 `thread_id` Resume；
- 记录节点执行次数，验证 `node_a` 不重复执行；
- 将实际验证过的核心依赖版本锁定；
- 记录 Spike 结论和已知限制。

## 7.3 测试

- 首次运行在 interrupt 处暂停；
- SQLite 文件存在且包含对应线程状态；
- 不依赖进程内存即可恢复；
- Resume 输入能够传回中断节点；
- Resume 后进入 `node_b` 并完成；
- 已完成节点无重复副作用。

## 7.4 完成定义

只有同时满足以下条件，才能进入 Day 1：

- 相同 `thread_id` 跨进程恢复成功；
- Checkpoint 数据可持久读取；
- 已完成节点没有重新执行；
- 依赖版本已经锁定；
- 恢复调用方式已形成最小可复用样例。

若 Spike 失败，暂停业务开发，优先查明版本兼容性、Checkpointer API 或 interrupt 语义问题。

---

# 8. Day 1：Core Business Flow

## 8.1 目标

建立不含动态调查和完整 HITL 的最小业务纵向链路：

```text
Financial
  ↓
Risk
  ↓
Report
```

## 8.2 任务

### 配置与 Schema

- 建立 `app/config.py`；
- 建立 `.env.example`；
- 配置模型、路径、阈值、重试和故障注入字段；
- 定义 Company、Financial、Risk、Trace Pydantic 模型；
- 定义 `RiskLevel`、`HumanDecision` 等 Enum。

### Case 数据

- 准备 Case A 与 Case B 的最小结构化源数据；
- 确保两组数据能够稳定触发预期财务表现；
- 对数据字段和年份顺序作统一约束。

### Financial Tool

- 实现 Revenue Growth；
- 实现 Net Profit Margin；
- 实现 Current Ratio；
- 实现 Debt Ratio；
- 实现 Operating Cash Flow Trend；
- 每项输出包含指标名、数值序列、单位和公式；
- 明确零除、缺失字段和年份错序的处理方式。

### Financial / Risk / Report

- Financial Agent 调用 Python Tool，不让 LLM 自行计算；
- Risk Agent 输出符合 Pydantic 约束的风险结构；
- 报告采用固定章节模板；
- Prompt 明确禁止给出最终贷款批准或拒绝决定；
- Prompt 文件全部放入 `app/prompts/`。

## 8.3 测试

- 每个财务公式有正常值测试；
- 对零分母、缺失值、年份顺序做边界测试；
- Risk 输出能够通过 Pydantic 校验；
- 报告包含规定章节且不包含自动授信决策。

## 8.4 完成定义

- 两个 Case 均可独立完成 Financial → Risk → Report；
- 财务计算结果可通过固定期望值断言；
- LLM 不承担确定性算术；
- 所有业务输出符合 Schema。

---

# 9. Day 2：Document、Artifact 与 Dynamic Routing

## 9.1 目标

完成数据结构化、Artifact 管理及由异常状态驱动的分支执行。

## 9.2 任务

### Document Agent

- 读取 `company_profile.json`、`business_info.md` 和财务源数据；
- 转换为统一企业结构；
- 输出 `company_profile_v1.json` Artifact。

### Artifact Store

- 建立 Case 级 `source/`、`artifacts/`、`output/`；
- 实现 JSON、Markdown Artifact 的读写；
- 返回稳定 Artifact Reference；
- 防止路径越界；
- 首版至少支持显式 `_v1` 版本，自动递增属于 P2。

### Graph State

State 仅保留：

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
external_research_incomplete
```

### Anomaly Detection

- 实现 `REVENUE_CASHFLOW_DIVERGENCE`；
- 实现 `DEBT_RATIO_RISING`；
- 所有阈值来自配置；
- 异常判断仅使用代码规则。

### Dynamic Routing

```text
anomaly_flags 为空    → risk
anomaly_flags 非空    → research
```

- 路由函数保持纯函数特征；
- 在测试中记录实际 execution path。

## 9.3 测试

- Artifact 写入、读取和引用解析；
- Artifact 内容不直接进入 Graph State；
- Case A 不生成异常标记；
- Case B 稳定生成目标异常标记；
- Case A 路由至 Risk；
- Case B 路由至 Research。

## 9.4 完成定义

```text
Case A: Document → Financial → Risk
Case B: Document → Financial → Research → Risk
```

两个执行路径必须由 State 决定，并能通过自动化测试稳定复现。

---

# 10. Day 3：Research Agent 与 MCP

## 10.1 目标

只将外部调查能力 MCP 化，以最小范围验证协议解耦。

## 10.2 任务

- 建立 Research MCP Server；
- 提供 `search_company()`；
- 提供 `search_industry()`；
- 使用 Mock Dataset 或本地 Search Adapter，保证演示稳定；
- 建立 Research MCP Client；
- Research Agent 根据异常标记组织查询；
- 将事实、来源标识、查询状态写入 Research Artifact；
- 不把完整调查结果塞入 Graph State；
- 确认 Case A 不触发无意义 MCP 调用。

## 10.3 测试

- 两个 MCP Tool 能独立调用；
- 未知公司或行业返回显式空结果，而非伪造事实；
- Case B 自动触发 Research；
- Research Artifact 能被 Risk Agent 引用；
- MCP Server 不可用时错误可识别并可交给后续 Retry 处理。

## 10.4 完成定义

- Case B 通过 MCP 获得补充事实；
- Risk Agent 能结合财务异常和调查事实生成结构化风险；
- Case A 保持不经过 Research 的路径。

---

# 11. Day 4：HITL 与完整跨进程 Resume

## 11.1 目标

完成项目最重要的端到端演示：任务暂停、程序退出、重新启动、人工决策、继续执行。

## 11.2 任务

### HITL

- `MEDIUM` 或 `HIGH` 风险进入 `WAITING_APPROVAL`；
- 审核界面展示风险项及证据；
- 支持 `APPROVE`：继续生成报告；
- 支持 `RESEARCH`：补充调查后重新分析；
- 保存 `human_decision` 和 `human_comment`。

### Interrupt 安全

- interrupt 节点只读取 State、生成审核内容并暂停；
- interrupt 前不发送不可重复请求；
- interrupt 前不创建不可幂等业务记录；
- Resume 后才执行决策对应的后续动作。

### Resume

- 提供按 `thread_id` 查询和恢复任务的入口；
- 新进程能够读取 SQLite Checkpoint；
- 恢复后保持 Artifact Reference 有效；
- 状态生命周期正确变化：

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

### UI

- 优先接入 Chainlit 最小页面；
- 展示当前任务 ID、状态、风险和审核操作；
- 若 Chainlit + HITL 联调超过约 2 小时仍不稳定，立即降级为 CLI + `thread_id` 输入。

## 11.3 测试

- Case B 到达 interrupt；
- 暂停时状态为 `WAITING_APPROVAL`；
- 完全结束进程；
- 新进程用相同 `thread_id` 恢复；
- APPROVE 路径生成报告；
- RESEARCH 路径重新调查且 Artifact 不覆盖历史结果；
- 已完成节点不重复执行；
- 最终状态为 `COMPLETED`。

## 11.4 完成定义

以下 Demo 必须连续成功：

```text
Start Case B
→ Document
→ Financial
→ Research
→ Risk
→ Interrupt
→ Kill Process
→ Restart
→ Resume(thread_id, human_decision)
→ Report
→ COMPLETED
```

---

# 12. Day 5：Retry、Fault Injection、Trace 与 Regression

## 12.1 目标

补齐基础故障恢复能力和可审计执行记录，并建立最小回归防线。

## 12.2 任务

### Retry

- Retry 范围仅限 Research Tool；
- `MAX_RETRY` 配置化，默认值为 2；
- 仅对明确的临时错误重试；
- 每次重试写入 Trace；
- 重试全部失败后设置 `external_research_incomplete = true`；
- Risk 和 Report 明确披露调查不完整。

### Fault Injection

- 使用 `RESEARCH_FAIL_FIRST=1`；
- 首次 Research 调用稳定抛出 `TimeoutError`；
- 第二次调用稳定成功；
- 故障注入状态按 task/tool 维度管理，避免测试相互污染；
- 禁止随机故障。

### JSONL Trace

每个事件记录：

```text
task_id
node
event_type
input_summary
output_summary
start_time
end_time
latency_ms
status
error
```

- 覆盖 Node 开始/结束、Tool 调用、Retry、Interrupt、Resume；
- Trace 中不写入 API Key 或完整敏感原文；
- 保证每行都是可独立解析的 JSON。

### Regression Cases

- `normal_01`；
- `normal_02`；
- `risk_cashflow`；
- `risk_debt`；
- `tool_failure`。

## 12.3 测试

- Routing 测试；
- Financial 公式测试；
- Retry 次数和最终状态测试；
- Trace Schema 和 JSONL 可解析性测试；
- Artifact 测试；
- Resume 集成测试；
- 五个 Regression Cases。

## 12.4 完成定义

- 故障注入行为可重复；
- 首次失败、一次重试后成功；
- 全部失败时没有静默忽略；
- 所有核心测试通过；
- Trace 可以重建任务关键执行路径。

---

# 13. Day 6：Showcase 与交付收尾

## 13.1 目标

将已完成系统整理成可运行、可解释、可复现的技术作品。

## 13.2 任务

- 完善 README；
- 说明安装、配置、启动、暂停和恢复命令；
- 添加 Mermaid 系统架构图和执行时序图；
- 编写 Case A、Case B、Retry、Resume 演示脚本；
- 记录已验证依赖和运行环境；
- 整理常见故障排查；
- 清理死代码、重复配置和过期命名；
- 检查仓库中不存在密钥、Checkpoint 运行数据或敏感 Trace；
- 整理简历表述及核心面试问题。

## 13.3 完成定义

一个不了解内部实现的开发者能够只根据 README：

1. 安装依赖；
2. 配置模型；
3. 运行两个 Case；
4. 触发并观察 Retry；
5. 在 interrupt 后停止进程；
6. 重启并恢复相同任务；
7. 找到 Artifact、Trace 和最终报告。

---

# 14. 测试策略

## 14.1 测试分层

| 层级 | 测试对象 | 重点 |
|---|---|---|
| 单元测试 | 财务公式、异常规则、路由、Schema | 确定性和边界条件 |
| 组件测试 | Artifact Store、MCP Tool、Trace | I/O 契约和错误处理 |
| 集成测试 | Graph 主流程、Retry、HITL | 节点协作和状态变化 |
| 跨进程测试 | Checkpoint / Resume | 进程退出后的持久恢复 |
| 回归测试 | 五个固定 Case | 路径和结果稳定性 |

## 14.2 优先级

测试优先顺序：

```text
test_resume
  ↓
test_routing
  ↓
test_financial
  ↓
test_retry
  ↓
Artifact / Trace / Schema
```

## 14.3 关键断言

- Case A 不经过 Research；
- Case B 经过 Research；
- 财务值与手工预期一致；
- 异常标记由规则稳定产生；
- interrupt 时 Checkpoint 已落盘；
- Resume 后不重复已完成节点；
- Retry 次数符合配置；
- 调查失败状态进入风险分析和报告；
- 报告不输出最终授信批准/拒绝结论。

---

# 15. 配置计划

`.env.example` 至少包含：

```text
MODEL_NAME=
MODEL_API_KEY=

CHECKPOINT_DB_PATH=checkpoints/credra_agent.db

MAX_RETRY=2
RESEARCH_FAIL_FIRST=0

DEBT_RATIO_THRESHOLD=
CASHFLOW_THRESHOLD=
REVENUE_THRESHOLD=

DATA_DIR=data
TRACE_DIR=traces
```

要求：

- 配置加载失败时给出明确错误；
- 阈值有类型和范围校验；
- 运行路径基于项目根目录解析；
- `.env` 不进入版本控制；
- 测试通过独立配置隔离运行数据。

---

# 16. Artifact 与状态设计计划

## 16.1 Artifact 命名

```text
company_profile_v1.json
financial_analysis_v1.json
research_result_v1.json
risk_analysis_v1.json
credit_report.md
```

需要重新调查或重新分析时，生成新的版本，不覆盖已被 State 引用的历史 Artifact。

## 16.2 State 最小化

State 中禁止保存：

- 完整源文件；
- 完整财务报表；
- 完整 Research 结果；
- 完整报告；
- 全量会话历史。

State 中仅保存 Artifact Reference、路由字段、任务状态、异常标记和人工决策。

## 16.3 幂等性

- Artifact 写入使用确定的任务目录；
- interrupt 前不执行不可重复副作用；
- 节点重新进入时能够判断既有产物；
- Report 生成发生在 Human Decision 之后；
- Trace 允许重复事件但必须带可识别的事件时间和状态。

---

# 17. 风险与应对

| 风险 | 影响 | 应对措施 | 降级方案 |
|---|---|---|---|
| LangGraph interrupt/checkpoint 版本不兼容 | 阻塞核心能力 | Day 0 先做 Spike 并锁版本 | 暂停业务开发，调整兼容版本 |
| Chainlit 与 HITL 状态同步复杂 | 延误完整链路 | UI 最后接入，限制联调时间 | CLI + `thread_id` |
| MCP SDK API 变化 | Research 延误 | 仅实现两个 Tool，锁定版本 | 保留本地 Adapter，确保主链路可测 |
| LLM 输出不符合 Schema | Risk/Report 失败 | 结构化输出、校验、有限修复 | 使用确定性 Mock/Fixture 做测试 |
| Resume 导致节点重复副作用 | 数据重复 | interrupt 前无副作用，节点幂等 | 通过 Artifact 版本和执行记录去重 |
| Artifact 与 State 不一致 | 恢复失败 | 写入成功后再更新引用 | 启动时校验引用并显式报错 |
| 外部调查失败被忽略 | 报告误导 | 显式 incomplete 标记 | 报告披露证据缺口 |
| 开发周期不足 | P0 延误 | 严格按优先级开发 | 舍弃 UI 美化、自动版本和部分 P1 |

---

# 18. 每日开发节奏

每个开发日遵循以下节奏：

1. 开始前确认上一阶段门禁已通过；
2. 先补当前功能的确定性测试或验收脚本；
3. 完成最小实现；
4. 运行当前阶段测试及已有回归；
5. 手工执行一次关键路径；
6. 更新 README/开发记录中的实际行为；
7. 记录未解决问题，不将临时行为默认为最终设计。

每个阶段结束时必须留下：

- 可运行产物；
- 自动化测试或明确验收命令；
- 已知限制；
- 下一阶段可依赖的稳定接口。

---

# 19. MVP最终验收清单

## P0

- [ ] 项目命名已统一为 Credra Agent
- [ ] Case A 完整运行
- [ ] Case B 完整运行
- [ ] 两个 Case 执行路径不同
- [ ] 财务指标由 Python 计算
- [ ] Anomaly Flags 由规则生成
- [ ] 中间结果使用 Artifact
- [ ] Graph State 不保存大块数据
- [ ] Risk Level 使用 Pydantic Enum
- [ ] HITL 可以暂停
- [ ] Checkpoint 写入 SQLite
- [ ] 完全重启程序后可以 Resume
- [ ] Resume 后不重复已完成节点
- [ ] 最终生成 Markdown 报告
- [ ] 报告不包含自动贷款批准/拒绝决定

## P1

- [ ] Research 通过 MCP 调用
- [ ] Research Timeout 可 Retry
- [ ] Fault Injection 可控且非随机
- [ ] 全部调查失败时显式记录 incomplete
- [ ] Trace 写入合法 JSONL
- [ ] 五个 Regression Cases 通过
- [ ] Prompt 全部外置

## P2

- [ ] Chainlit 完整 UI
- [ ] Artifact 自动版本递增
- [ ] 统一开发任务命令
- [ ] Demo 录屏

---

# 20. 推荐实施顺序

正式开始编码时，严格从以下任务开始：

```text
1. Day 0 Checkpoint / Interrupt / Resume Spike
2. 锁定实际验证过的依赖版本
3. 财务工具与 Schema
4. 最小 Financial → Risk → Report
5. Artifact 与动态路由
6. Research MCP
7. 完整 HITL 与跨进程恢复
8. Retry、Trace 和 Regression
9. Chainlit 与 Showcase
```

在 Day 0 门禁通过前，不开始 Agent 角色、MCP 或 UI 的批量实现。

