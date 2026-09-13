"""Human-readable projection of the same bounded, sanitized service events."""

EVENT_LABELS = {
    "SERVICE_START": "服务启动",
    "CONFIG_VALIDATED": "配置校验",
    "SERVICE_READY": "服务就绪",
    "SERVICE_STOP": "服务停止",
    "PROCESS_START": "子进程启动",
    "PROCESS_STOP": "子进程停止",
    "UNCAUGHT_EXCEPTION": "未处理异常",
    "SDK_DIAGNOSTIC": "Python/SDK 诊断",
    "REQUEST_ACCEPTED": "请求接受",
    "REQUEST_END": "请求结束",
    "REQUEST_REJECTED": "请求拒绝",
    "TASK_START": "任务启动",
    "TASK_STATE": "任务状态",
    "TASK_END": "任务结束",
    "STATUS_QUERY": "状态查询",
    "RESUME": "恢复任务",
    "INTERRUPT": "等待人工处理",
    "NODE_START": "进入节点",
    "NODE_END": "节点结束",
    "NODE_SKIP": "跳过节点",
    "ROUTE": "路由决策",
    "PLAN_CHANGED": "计划变更",
    "ACTION_STATE": "Agent 动作状态",
    "BUDGET_STATE": "预算状态",
    "STOP": "停止新动作",
    "RETRY": "调用重试",
    "DEGRADED": "降级处理",
    "LLM_ATTEMPT_START": "LLM 请求开始",
    "LLM_FIRST_TOKEN": "LLM 首 Token",
    "LLM_ATTEMPT_END": "LLM 请求返回",
    "LLM_VALIDATION": "LLM 输出校验",
    "MCP_CONNECT": "MCP 连接",
    "MCP_DISCONNECT": "MCP 断开",
    "TOOL_START": "工具调用开始",
    "TOOL_END": "工具调用结束",
    "SOURCE_RESULT": "消息来源结果",
    "CHECKPOINT_OPEN": "Checkpoint 打开",
    "CHECKPOINT_READ": "Checkpoint 读取",
    "CHECKPOINT_SAVE": "Checkpoint 保存",
    "CHECKPOINT_CLOSE": "Checkpoint 关闭",
    "CHECKPOINT_ERROR": "Checkpoint 异常",
    "ARTIFACT_READ": "Artifact 读取",
    "ARTIFACT_WRITE": "Artifact 写入",
    "ARTIFACT_ERROR": "Artifact 异常",
    "REPORT_WRITE": "报告写入",
    "AUDIT_EXPORT": "审计导出",
    "TRACE_MIRROR": "业务 Trace 关联",
    "LOG_RECOVERY": "日志恢复",
    "LOG_GAP": "日志完整性缺口",
}

NODE_LABELS = {
    "document": "资料读取",
    "financial": "财务分析",
    "research": "外部调查",
    "risk": "风险分析",
    "report": "报告生成",
    "decide": "Agent 决策",
    "execute": "Agent 执行",
    "intent": "自然语言解析",
    "ui_runtime": "UI 执行入口",
}

ISSUE_LABELS = {
    "missing": "缺少必填字段",
    "extra_forbidden": "存在 Schema 不允许的字段",
    "literal_error": "字段取值不在允许范围",
    "string_too_long": "文本超过长度上限",
    "string_too_short": "文本短于长度下限",
    "too_long": "列表/对象超过长度上限",
    "too_short": "列表/对象短于长度下限",
    "string_type": "应为字符串",
    "list_type": "应为列表",
    "dict_type": "应为对象",
    "int_type": "应为整数",
    "int_parsing": "无法解析为整数",
    "float_type": "应为数值",
    "bool_type": "应为布尔值",
    "value_error": "自定义约束不满足",
    "string_pattern_mismatch": "字段格式不满足约束",
    "other": "其他 Schema 约束不满足",
}


def _reference(value):
    if isinstance(value, str) and value.startswith("ref_"):
        return value[:20]
    return value


def render_event(record: dict) -> bytes:
    """Input is sanitized before collection; never read model/provider payloads."""
    kind = record.get("event_type", "")
    node = record.get("node")
    parts = [
        record.get("timestamp_utc", ""),
        f"[{record.get('level', 'INFO')}]",
        f"#{record.get('sequence', '?')}",
        f"[{record.get('service', 'service')}]",
        EVENT_LABELS.get(kind, kind or "服务事件"),
    ]
    if node:
        parts.append(f"节点={NODE_LABELS.get(node, node)}({node})")
    fields = {
        "status": "状态",
        "purpose": "用途",
        "phase": "阶段",
        "validation_stage": "校验阶段",
        "attempt": "尝试",
        "duration_ms": "耗时ms",
        "error_code": "错误码",
        "http_status": "HTTP",
        "finish_reason": "结束原因",
        "model": "模型引用",
        "provider": "Provider引用",
        "tool": "工具",
        "target_node": "目标节点",
        "from_state": "前状态",
        "to_state": "后状态",
        "plan_version": "计划版本",
        "task_spec_version": "TaskSpec版本",
        "result_count": "结果数",
        "output_chars": "输出字符数",
        "reasoning_chars": "思考字符数",
        "external_requests": "外部请求数",
        "token_units": "预算Token",
        "thread_id": "任务引用",
        "run_id": "运行引用",
        "call_id": "调用引用",
        "action_id": "动作引用",
        "request_id": "请求引用",
        "artifact_ref": "产物引用",
        "trace_event_ref": "Trace引用",
        "trace_line": "Trace行",
        "pid": "PID",
    }
    for key, label in fields.items():
        if key in record:
            value = record[key]
            parts.append(
                f"{label}={_reference(value) if value is not None else '未知'}"
            )
    if kind.startswith("LLM_"):
        for key, label in (
            ("input_tokens", "输入Token"),
            ("output_tokens", "输出Token"),
        ):
            value = record.get(key)
            parts.append(f"{label}={value if value is not None else '未知'}")
    if kind == "LLM_ATTEMPT_END" and record.get("status") == "SUCCESS":
        parts.append("说明=传输返回成功；输出是否可用请查看后续校验")
    if kind == "RETRY":
        parts.append("说明=重试事件本身不代表请求耗时；请查看 LLM 请求返回记录")
    lines = [" | ".join(parts)]
    for issue in record.get("validation_issues", []):
        path = ".".join(str(part) for part in issue["path"]) or "<root>"
        detail = f"  字段={path} | {ISSUE_LABELS[issue['kind']]} ({issue['kind']})"
        for key, label in (("limit", "约束长度"), ("actual_length", "实际长度")):
            if key in issue:
                detail += f" | {label}={issue[key]}"
        lines.append(detail)
    if "validation_issue_count" in record:
        lines.append(
            f"  Schema错误总数={record['validation_issue_count']}"
            f" | 已记录={len(record.get('validation_issues', []))}"
        )
    if "json_error_position" in record:
        lines.append(
            f"  JSON解析位置={record['json_error_position']}"
            f" | 行={record.get('json_error_line', '未知')}"
            f" | 列={record.get('json_error_column', '未知')}"
        )
    for frame in record.get("stack", []):
        lines.append(
            f"  堆栈 文件引用={_reference(frame['file_ref'])}"
            f" | 行={frame['line']} | 函数引用={_reference(frame['function_ref'])}"
        )
    # Only raw writer boundary tests supply a free-form message. Production IPC
    # accepts the static event message only; retain this field for volume tests.
    if not kind and record.get("message"):
        lines.append(record["message"])
    return ("\n".join(lines) + "\n").encode("utf-8")
