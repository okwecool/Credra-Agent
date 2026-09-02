"""Read-only, bounded workbench projections over run artifacts and traces."""

import html
import ipaddress
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.models.trace import TraceEvent
from app.tools.artifacts import ArtifactStore

EVIDENCE_DISPLAY_LIMIT = 12
TRACE_DISPLAY_LIMIT = 16
TRACE_READ_LIMIT = 200
TRACE_MAX_BYTES = 512_000
REPORT_MAX_BYTES = 2_000_000
REPORT_PREVIEW_LIMIT = 12_000
_RUN_ID = re.compile(r"^[0-9a-f]{20}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_VALUE = re.compile(r"(?i)(api[_-]?key|authorization|bearer)(\s*[:=]\s*)\S+")
_METRIC_LABELS = {
    "revenue_growth": "营收增长率",
    "net_profit_margin": "净利润率",
    "current_ratio": "流动比率",
    "debt_ratio": "资产负债率",
    "operating_cash_flow_trend": "经营现金流",
}


def _display_text(value: Any, *, limit: int = 240) -> str:
    """Render untrusted text as one escaped, bounded Markdown fragment."""

    text = _CONTROL_CHARS.sub("", str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("`", "'")
    for character in ("\\", "[", "]", "<", ">", "|"):
        text = text.replace(character, "\\" + character)
    return text or "—"


def friendly_error_summary(value: Any, *, limit: int = 300) -> str:
    """Classify operational failures without exposing credentials or internals."""

    raw = _SENSITIVE_VALUE.sub(r"\1\2[REDACTED]", str(value or ""))
    lowered = raw.lower()
    if any(
        token in lowered for token in ("api key", "api_key", "tavily_api_key")
    ) and any(token in lowered for token in ("missing", "required", "not configured")):
        message = "搜索配置缺失：请检查用户维护的模型或搜索 Provider 配置。"
    elif any(
        token in lowered
        for token in ("401", "403", "unauthorized", "forbidden", "invalid api key")
    ):
        message = "外部服务认证失败：请检查 Provider 地址、Key 和权限。"
    elif any(
        token in lowered
        for token in ("429", "432", "rate limit", "quota", "credit", "额度")
    ):
        message = "外部服务额度不足或触发限流，请稍后重试或检查账户额度。"
    elif any(token in lowered for token in ("timeout", "timed out", "超时")):
        message = "外部服务响应超时；任务已保留证据缺口，可稍后补充调查。"
    elif any(
        token in lowered
        for token in ("connection", "network", "dns", "winerror", "网络")
    ):
        message = "外部服务网络连接失败；请检查网络与 Provider 可达性。"
    elif "snapshot" in lowered and any(
        token in lowered for token in ("missing", "not found", "缺失")
    ):
        message = "离线快照缺失：当前 Query 无法完成 Snapshot 回放。"
    else:
        message = raw
    return _CONTROL_CHARS.sub("", message)[:limit]


def _safe_http_url(value: Any) -> str | None:
    """Keep only Markdown-safe public link schemes; content remains untrusted."""

    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() or character in "<>" for character in value)
    ):
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return value


def _run_dir(payload: dict[str, Any], settings: Settings) -> Path:
    state = payload.get("state", {})
    case_id = state.get("case_id")
    if not isinstance(case_id, str) or not re.fullmatch(
        r"^[a-z][a-z0-9_]{2,63}$", case_id
    ):
        raise ValueError("invalid case id in task state")
    data_root = settings.data_dir.resolve()
    case_dir = (data_root / case_id).resolve()
    if case_dir.parent != data_root or not (case_dir / "source").is_dir():
        raise ValueError("case directory is unavailable")
    run_id = state.get("run_id")
    if run_id is None:
        return case_dir
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id in task state")
    return case_dir / "runs" / run_id


def _artifact_history(run_dir: Path) -> list[str]:
    artifact_dir = run_dir / "artifacts"
    if not artifact_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in artifact_dir.iterdir()
        if path.is_file()
        and re.fullmatch(r"[a-z][a-z0-9_]*_v\d+\.(json|md)", path.name)
    )


def _read_trace_tail(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.is_file():
        return [], False
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > TRACE_MAX_BYTES:
            file.seek(-TRACE_MAX_BYTES, 2)
            file.readline()
        content = file.read().decode("utf-8")
    events = [
        TraceEvent.model_validate(json.loads(line)).model_dump(mode="json")
        for line in content.splitlines()
        if line.strip()
    ]
    truncated = size > TRACE_MAX_BYTES or len(events) > TRACE_READ_LIMIT
    return events[-TRACE_READ_LIMIT:], truncated


def _read_report_markdown(run_dir: Path, reference: str) -> str:
    normalized = PurePosixPath(reference)
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or len(normalized.parts) != 2
        or normalized.parts[0] != "output"
        or not re.fullmatch(r"[a-z][a-z0-9_-]*\.md", normalized.parts[1])
    ):
        raise ValueError("invalid report reference")
    output_dir = (run_dir / "output").resolve()
    path = (run_dir / Path(*normalized.parts)).resolve()
    if path.parent != output_dir or not path.is_file():
        raise ValueError("report file is unavailable")
    if path.stat().st_size > REPORT_MAX_BYTES:
        raise ValueError("report file exceeds workbench limit")
    return path.read_text(encoding="utf-8")


def report_markdown_to_html(markdown: str) -> str:
    """Render the fixed report subset as standalone HTML with all text escaped."""

    body: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for raw_line in markdown.splitlines():
        line = _CONTROL_CHARS.sub("", raw_line).strip()
        if not line:
            close_list()
            continue
        heading = re.fullmatch(r"(#{1,3})\s+(.+)", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            body.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.startswith("> "):
            close_list()
            body.append(f"<blockquote>{html.escape(line[2:])}</blockquote>")
        else:
            close_list()
            body.append(f"<p>{html.escape(line)}</p>")
    close_list()
    return (
        """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Credra Agent 企业授信尽调分析报告</title>
<style>
body{max-width:960px;margin:40px auto;padding:0 24px;color:#202124;
font:16px/1.7 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1,h2,h3{line-height:1.3}h2{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.4rem}
blockquote{margin:2rem 0;padding:1rem;border-left:4px solid #777;background:#f6f7f8}
li{margin:.35rem 0}@media print{body{margin:0;max-width:none}}
</style>
</head>
<body>
"""
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    )


def load_workbench_details(
    payload: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Load bounded details from only the active run and its task trace."""

    details: dict[str, Any] = {
        "financial": None,
        "risk": None,
        "risk_narrative": None,
        "evidence_summary": None,
        "query_proposal": None,
        "query_plan": None,
        "research": None,
        "anomaly_flags": list(payload.get("state", {}).get("anomaly_flags") or []),
        "report_markdown": None,
        "report_html": None,
        "artifact_history": [],
        "trace_events": [],
        "trace_truncated": False,
        "warnings": [],
    }
    try:
        run_dir = _run_dir(payload, settings)
        store = ArtifactStore(run_dir)
        details["artifact_history"] = _artifact_history(run_dir)
        state = payload.get("state", {})
        for key, detail_key in (
            ("financial_artifact", "financial"),
            ("risk_artifact", "risk"),
            ("risk_narrative_artifact", "risk_narrative"),
            ("evidence_summary_artifact", "evidence_summary"),
            ("query_proposal_artifact", "query_proposal"),
            ("query_plan_artifact", "query_plan"),
            ("research_artifact", "research"),
        ):
            reference = state.get(key)
            if reference:
                try:
                    details[detail_key] = store.read_json(reference)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    details["warnings"].append(
                        f"{detail_key} 无法读取：{type(exc).__name__}"
                    )
        report_reference = state.get("report_artifact")
        if report_reference:
            try:
                markdown = _read_report_markdown(run_dir, report_reference)
                details["report_markdown"] = markdown
                details["report_html"] = report_markdown_to_html(markdown)
            except (OSError, ValueError) as exc:
                details["warnings"].append(f"report 无法读取：{type(exc).__name__}")
    except (OSError, ValueError) as exc:
        details["warnings"].append(f"运行目录不可用：{type(exc).__name__}")

    thread_id = payload.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        trace_root = settings.trace_dir.resolve()
        trace_path = (trace_root / f"{thread_id}.jsonl").resolve()
        if trace_path.parent != trace_root:
            details["warnings"].append("Trace 路径不安全，已拒绝读取")
        else:
            try:
                events, truncated = _read_trace_tail(trace_path)
                details["trace_events"] = events
                details["trace_truncated"] = truncated
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                details["warnings"].append(f"Trace 无法读取：{type(exc).__name__}")
    return details


def _metric_values(metric_name: str, metric: dict[str, Any]) -> str:
    values: list[str] = []
    for year, raw_value in zip(metric.get("years", []), metric.get("values", [])):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            rendered = _display_text(raw_value, limit=30)
        else:
            if metric_name in {"revenue_growth", "net_profit_margin", "debt_ratio"}:
                rendered = f"{value:.2%}"
            elif metric_name == "current_ratio":
                rendered = f"{value:.2f}x"
            else:
                rendered = f"{value:,.2f}"
        values.append(f"{_display_text(year, limit=8)}：{rendered}")
    return "；".join(values) or "—"


def financial_risk_markdown(details: dict[str, Any]) -> list[str]:
    lines = ["## 财务与风险摘要"]
    financial = details.get("financial")
    if financial:
        lines.extend(
            (
                f"- 币种/单位：`{_display_text(financial.get('currency'))}`",
                "",
                "| 指标 | 年度结果 | 计算公式 |",
                "|---|---|---|",
            )
        )
        for metric_name, metric in (financial.get("metrics") or {}).items():
            label = _METRIC_LABELS.get(metric_name, metric_name)
            lines.append(
                f"| {_display_text(label)} | {_metric_values(metric_name, metric)} | "
                f"{_display_text(metric.get('formula'), limit=100)} |"
            )
    else:
        lines.append("_财务分析尚未生成。_")

    anomaly_flags = details.get("anomaly_flags") or []
    lines.append(
        "- 异常标记："
        + (
            ", ".join(f"`{_display_text(flag)}`" for flag in anomaly_flags)
            if anomaly_flags
            else "无"
        )
    )
    risk = details.get("risk")
    if not risk:
        lines.append("_风险分析尚未生成。_")
        return lines
    lines.extend(
        (
            f"- 风险等级：`{_display_text(risk.get('risk_level'))}`",
            f"- 综合摘要：{_display_text(risk.get('summary'))}",
            "### 风险项",
        )
    )
    flags = risk.get("risk_flags") or []
    if not flags:
        lines.append("_未识别出显著风险项。_")
    for flag in flags:
        evidence = ", ".join(
            f"`{_display_text(item, limit=100)}`" for item in flag.get("evidence", [])
        )
        lines.append(
            f"- **{_display_text(flag.get('severity'))} · "
            f"{_display_text(flag.get('type'))}**："
            f"{_display_text(flag.get('description'))}  \n  证据：{evidence}"
        )
    return lines


def llm_narrative_markdown(details: dict[str, Any]) -> list[str]:
    """Render only citation-checked narrative fields from the active run."""

    narrative = details.get("risk_narrative")
    if not narrative:
        return []
    lines = ["## LLM 风险解释"]
    lines.append(
        "- 模式："
        f"`{_display_text(narrative.get('mode'))}` · 执行："
        f"`{_display_text(narrative.get('execution_status'))}` · 模型："
        f"`{_display_text(narrative.get('model_name'), limit=100)}`"
    )
    lines.append(
        "- Prompt："
        f"`{_display_text(narrative.get('prompt_version'), limit=100)}` · "
        f"尝试 `{int(narrative.get('attempts') or 0)}` 次 · "
        f"Token `{narrative.get('input_tokens') or 0}` / "
        f"`{narrative.get('output_tokens') or 0}`"
    )
    if narrative.get("execution_status") == "DEGRADED":
        lines.append(
            "- 已安全降级："
            f"`{_display_text(narrative.get('error_code'), limit=80)}`；"
            "仍保留确定性风险结论，未让模型错误改变风险等级或路由。"
        )
    lines.append(
        f"- 综合解释：{_display_text(narrative.get('overall_summary'), limit=800)}"
    )
    explanations = narrative.get("explanations") or []
    if explanations:
        lines.append("### 对应风险项")
    for explanation in explanations:
        evidence = ", ".join(
            f"`{_display_text(item, limit=100)}`"
            for item in explanation.get("evidence_ids", [])
        )
        lines.append(
            f"- `{_display_text(explanation.get('risk_id'), limit=100)}`："
            f"{_display_text(explanation.get('explanation'), limit=800)}  \n"
            f"  允许证据：{evidence or '—'}"
        )
    limitations = narrative.get("limitations") or []
    if limitations:
        lines.append(
            "- 边界说明："
            + "；".join(_display_text(item, limit=300) for item in limitations)
        )
    return lines


def _safe_report_preview(markdown: str) -> tuple[str, bool]:
    truncated = len(markdown) > REPORT_PREVIEW_LIMIT
    source = markdown[:REPORT_PREVIEW_LIMIT]
    lines: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        heading = re.fullmatch(r"(#{1,3})\s+(.+)", line)
        if heading:
            lines.append(
                f"{heading.group(1)} {_display_text(heading.group(2), limit=500)}"
            )
        elif line.startswith("- "):
            lines.append(f"- {_display_text(line[2:], limit=500)}")
        elif line.startswith("> "):
            lines.append(f"> {_display_text(line[2:], limit=500)}")
        else:
            lines.append(_display_text(line, limit=500))
    return "\n".join(lines), truncated


def report_preview_markdown(details: dict[str, Any]) -> list[str]:
    report = details.get("report_markdown")
    if not report:
        return [
            "## 报告预览与下载",
            "_报告尚未生成；风险任务需先完成人工批准。_",
        ]
    preview, truncated = _safe_report_preview(report)
    lines = [
        "## 报告预览与下载",
        "下载文件：`credit_report.md`、`credit_report.html`",
        "",
        preview,
    ]
    if truncated:
        lines.append("> 页面预览已截断，下载文件包含完整报告。")
    return lines


def _query_result_markdown(label: str, result: dict[str, Any]) -> list[str]:
    lines = [
        f"### {label}",
        (
            f"- 执行：`{_display_text(result.get('status'))}` · "
            f"来源：`{_display_text(result.get('source'))}` · "
            f"核验：`{_display_text(result.get('verification_status'))}`"
        ),
        (
            f"- Raw `{result.get('raw_result_count', 0)}` · "
            f"Candidate `{len(result.get('candidate_evidence', []))}` · "
            f"Verified Fact `{len(result.get('facts', []))}` · "
            f"Rejected `{result.get('rejected_result_count', 0)}`"
        ),
        (
            f"- 正文获取：`{_display_text(result.get('content_fetch_status'))}` · "
            f"核验执行：`{_display_text(result.get('verification_execution_status'))}`"
        ),
    ]
    if result.get("error"):
        lines.append(
            f"- 操作提示：{_display_text(friendly_error_summary(result['error']))}"
        )
    return lines


def _evidence_items(research: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result_key in ("company_result", "industry_result"):
        result = research.get(result_key) or {}
        for collection in ("evidence", "candidate_evidence"):
            for item in result.get(collection, []):
                source_id = str(item.get("source_id") or "")
                identity = source_id or str(item.get("source_url") or id(item))
                if identity not in seen:
                    seen.add(identity)
                    items.append(item)
    return items


def _evidence_markdown(item: dict[str, Any], index: int) -> list[str]:
    title = _display_text(item.get("title"), limit=120)
    safe_url = _safe_http_url(item.get("source_url"))
    source = f"[打开来源](<{safe_url}>)" if safe_url else "无安全可打开 URL"
    lines = [
        f"#### {index}. {title}",
        (
            f"- {source} · Tier `{_display_text(item.get('source_tier'))}` · "
            f"Stage `{_display_text(item.get('evidence_stage'))}` · "
            f"Status `{_display_text(item.get('verification_status'))}`"
        ),
        (
            f"- 主体 `{_display_text(item.get('subject_match'))}` · "
            f"类别匹配 `{bool(item.get('category_match'))}` · "
            f"相关度 `{item.get('relevance_score', 0):.3f}`"
        ),
    ]
    reasons = item.get("filter_reasons") or []
    if reasons:
        lines.append(
            "- 过滤原因：" + ", ".join(f"`{_display_text(x)}`" for x in reasons)
        )
    fetched = item.get("fetched_content") or {}
    if fetched:
        lines.append(
            f"- 正文：`{_display_text(fetched.get('status'))}` · "
            f"`{_display_text(fetched.get('content_kind'))}` · "
            f"位置 `{len(fetched.get('locations', []))}`"
        )
    claim = item.get("verification_claim") or {}
    if claim:
        lines.append(f"- Claim：{_display_text(claim.get('statement'))}")
    verification = item.get("verification") or {}
    if verification:
        lines.append(
            f"- Verifier：`{_display_text(verification.get('relation'))}` · "
            f"accepted `{bool(verification.get('accepted'))}` · "
            f"confidence `{verification.get('confidence', 0):.2f}` · "
            f"model `{_display_text(verification.get('verifier_model'))}`"
        )
        if verification.get("evidence_location"):
            lines.append(
                f"- 证据位置：`{_display_text(verification['evidence_location'])}`"
            )
        if verification.get("evidence_excerpt"):
            lines.append(
                f"- 证据短句：{_display_text(verification['evidence_excerpt'])}"
            )
        if verification.get("error_code"):
            lines.append(
                f"- 核验降级：`{_display_text(verification['error_code'])}` · "
                f"{_display_text(verification.get('error_message'))}"
            )
    return lines


def research_details_markdown(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    plan = details.get("query_plan")
    if plan:
        intent = plan.get("intent") or {}
        lines.extend(
            (
                "## 调查计划",
                f"- Intent：`{_display_text(intent.get('intent_id'))}`",
                "- 类别："
                + ", ".join(
                    f"`{_display_text(category)}`"
                    for category in intent.get("categories", [])
                ),
                "",
                "| 类型 | 类别 | 确定性 Query |",
                "|---|---|---|",
            )
        )
        for query in plan.get("queries", []):
            lines.append(
                f"| {_display_text(query.get('query_type'))} | "
                f"{_display_text(query.get('category'))} | "
                f"{_display_text(query.get('query'), limit=160)} |"
            )
        added = plan.get("added_queries") or []
        removed = plan.get("removed_queries") or []
        if added or removed:
            lines.append(f"- 相对上一版本：新增 `{len(added)}`，移除 `{len(removed)}`")

    research = details.get("research")
    if not research:
        return lines
    lines.extend(
        (
            "",
            "## Research / Evidence",
            (
                f"- 总体：执行 `{_display_text(research.get('execution_status'))}` · "
                f"核验 `{_display_text(research.get('verification_status'))}` · "
                f"Candidate `{research.get('candidate_count', 0)}` · "
                f"Verified Fact `{research.get('verified_fact_count', 0)}`"
            ),
        )
    )
    for label, key in (("企业调查", "company_result"), ("行业调查", "industry_result")):
        result = research.get(key)
        if result:
            lines.extend(_query_result_markdown(label, result))

    items = _evidence_items(research)
    if not items:
        lines.extend(("### Evidence 明细", "_本轮没有候选 Evidence。_"))
        return lines
    lines.append(
        f"### Evidence 明细（显示 {min(len(items), EVIDENCE_DISPLAY_LIMIT)}/{len(items)}）"
    )
    for index, item in enumerate(items[:EVIDENCE_DISPLAY_LIMIT], start=1):
        lines.extend(_evidence_markdown(item, index))
    if len(items) > EVIDENCE_DISPLAY_LIMIT:
        lines.append(
            f"> 其余 {len(items) - EVIDENCE_DISPLAY_LIMIT} 条保留在 Research Artifact 中。"
        )
    return lines


def _analysis_execution_lines(artifact: dict[str, Any]) -> list[str]:
    lines = [
        (
            "- 模式："
            f"`{_display_text(artifact.get('mode'))}` · 执行："
            f"`{_display_text(artifact.get('execution_status'))}` · 模型："
            f"`{_display_text(artifact.get('model_name'), limit=100)}`"
        ),
        (
            "- Prompt："
            f"`{_display_text(artifact.get('prompt_version'), limit=100)}` · "
            f"尝试 `{int(artifact.get('attempts') or 0)}` 次 · "
            f"Token `{artifact.get('input_tokens') or 0}` / "
            f"`{artifact.get('output_tokens') or 0}`"
        ),
    ]
    if artifact.get("execution_status") == "DEGRADED":
        lines.append(
            "- 已安全降级："
            f"`{_display_text(artifact.get('error_code'), limit=80)}`；"
            "确定性 Research 与规则 Query Plan 保持不变。"
        )
    return lines


def evidence_summary_markdown(details: dict[str, Any]) -> list[str]:
    summary = details.get("evidence_summary")
    if not summary:
        return []
    lines = ["## Evidence Summary（受约束 LLM）", *_analysis_execution_lines(summary)]
    lines.append(
        f"- 已核验摘要：{_display_text(summary.get('verified_summary'), limit=1_000)}"
    )
    references = summary.get("summary_evidence_ids") or []
    if references:
        lines.append(
            "- 摘要引用："
            + ", ".join(f"`{_display_text(item, limit=100)}`" for item in references)
        )
    entries = summary.get("entries") or []
    if entries:
        lines.append("### Evidence 状态")
    for entry in entries:
        bindings = [
            f"Evidence `{_display_text(entry.get('evidence_id'), limit=100)}`",
            f"Source `{_display_text(entry.get('source_id'), limit=100)}`",
        ]
        if entry.get("claim_id"):
            bindings.append(
                f"Claim `{_display_text(entry.get('claim_id'), limit=100)}`"
            )
        if entry.get("fact_id"):
            bindings.append(f"Fact `{_display_text(entry.get('fact_id'), limit=100)}`")
        lines.append(
            f"- **{_display_text(entry.get('verification_status'))} · "
            f"{_display_text(entry.get('category'))}**："
            f"{_display_text(entry.get('summary'), limit=800)}  \n"
            f"  {' · '.join(bindings)}"
        )
        source_category = entry.get("source_category")
        if source_category and source_category != entry.get("category"):
            lines.append(
                "  来源类别："
                f"`{_display_text(source_category, limit=100)}`（已映射到当前调查类别）"
            )
    gaps = summary.get("gaps") or []
    if gaps:
        lines.append("### 证据缺口")
    for gap in gaps:
        lines.append(
            f"- `{_display_text(gap.get('reason'))}` · "
            f"`{_display_text(gap.get('query_type'))}/"
            f"{_display_text(gap.get('category'))}`："
            f"{_display_text(gap.get('summary'), limit=700)}  \n"
            f"  Gap `{_display_text(gap.get('gap_id'), limit=100)}`"
        )
    limitations = summary.get("limitations") or []
    if limitations:
        lines.append(
            "- 边界说明："
            + "；".join(_display_text(item, limit=300) for item in limitations)
        )
    return lines


def query_proposal_markdown(details: dict[str, Any]) -> list[str]:
    proposal = details.get("query_proposal")
    if not proposal:
        return []
    lines = ["## Query Proposal（仅供人工审核）", *_analysis_execution_lines(proposal)]
    categories = proposal.get("allowed_categories") or []
    lines.append(
        "- 允许类别："
        + ", ".join(f"`{_display_text(category)}`" for category in categories)
    )
    items = proposal.get("proposals") or []
    if not items:
        lines.append("_本轮没有模型 Query 建议；规则 Query Plan 仍是唯一执行计划。_")
        return lines
    for item in items:
        references = ", ".join(
            f"`{_display_text(reference, limit=100)}`"
            for reference in item.get("reference_ids", [])
        )
        lines.append(
            f"### {_display_text(item.get('query_type'))} / "
            f"{_display_text(item.get('category'))} · "
            f"`{_display_text(item.get('decision'))}`"
        )
        lines.append(f"- 建议 Query：{_display_text(item.get('query'), limit=500)}")
        lines.append(f"- 关联引用：{references or '—'}")
        lines.append(
            "- 建议理由（不构成事实）："
            f"{_display_text(item.get('rationale'), limit=700)}"
        )
        reasons = item.get("rejection_reasons") or []
        if reasons:
            lines.append(
                "- 拒绝原因："
                + ", ".join(f"`{_display_text(reason)}`" for reason in reasons)
            )
    lines.append(
        "> 任何 `ACCEPTED_FOR_REVIEW` 建议也不会自动执行；当前规则 Query Plan 未被修改。"
    )
    return lines


def artifact_history_markdown(details: dict[str, Any]) -> list[str]:
    history = details.get("artifact_history") or []
    if not history:
        return []
    groups: dict[str, list[str]] = {}
    for name in history:
        match = re.fullmatch(r"(.+)_v(\d+)\.(json|md)", name)
        if match:
            groups.setdefault(match.group(1), []).append(f"v{match.group(2)}")
    return [
        "## Artifact 版本历史",
        *(
            f"- `{_display_text(name)}`：" + " → ".join(versions)
            for name, versions in sorted(groups.items())
        ),
    ]


def trace_markdown(details: dict[str, Any]) -> list[str]:
    events = details.get("trace_events") or []
    if not events:
        return ["## Retry / Trace", "_当前没有可读取的 Trace 事件。_"]
    status_counts = Counter(str(event.get("status")) for event in events)
    event_counts = Counter(str(event.get("event_type")) for event in events)
    lines = [
        "## Retry / Trace",
        (
            f"- 已读取 `{len(events)}` 个事件 · Retry `{event_counts['RETRY']}` · "
            f"Failed `{status_counts['FAILED']}` · Interrupt `{event_counts['INTERRUPT']}` · "
            f"Resume `{event_counts['RESUME']}`"
        ),
        "",
        "| 时间 | 节点 | 事件 | 状态 | 延迟 | 摘要 |",
        "|---|---|---|---|---:|---|",
    ]
    shown = events[-TRACE_DISPLAY_LIMIT:]
    for event in shown:
        summary = (
            event.get("error")
            or event.get("output_summary")
            or event.get("input_summary")
        )
        lines.append(
            f"| {_display_text(event.get('end_time'), limit=32)} | "
            f"{_display_text(event.get('node'), limit=30)} | "
            f"{_display_text(event.get('event_type'), limit=30)} | "
            f"{_display_text(event.get('status'), limit=20)} | "
            f"{int(event.get('latency_ms') or 0)} ms | "
            f"{_display_text(summary, limit=120)} |"
        )
    if len(events) > TRACE_DISPLAY_LIMIT or details.get("trace_truncated"):
        lines.append("> 页面仅展示最近事件；完整 Trace 保留在任务 JSONL 文件中。")
    return lines


def workbench_detail_markdown(details: dict[str, Any]) -> str:
    sections = [
        *financial_risk_markdown(details),
        *llm_narrative_markdown(details),
        *research_details_markdown(details),
        *evidence_summary_markdown(details),
        *query_proposal_markdown(details),
        *artifact_history_markdown(details),
        *trace_markdown(details),
        *report_preview_markdown(details),
    ]
    warnings = details.get("warnings") or []
    if warnings:
        sections.extend(
            (
                "## 工作台读取提示",
                *(f"- {_display_text(warning)}" for warning in warnings),
            )
        )
    return "\n".join(sections)
