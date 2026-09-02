"""M3-B workbench projections stay traceable, bounded, and traversal-safe."""

from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.models.trace import TraceStatus
from app.runtime.tracing import TraceWriter
from app.tools.artifacts import ArtifactStore
from app.workbench import (
    friendly_error_summary,
    load_workbench_details,
    workbench_detail_markdown,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        trace_dir=tmp_path / "traces",
        checkpoint_db_path=tmp_path / "credra.db",
    )


def _payload() -> dict:
    return {
        "thread_id": "workbench-001",
        "state": {
            "case_id": "case_demo",
            "run_id": "0123456789abcdef0123",
            "query_plan_artifact": "artifacts/query_plan_v2.json",
            "research_artifact": "artifacts/research_result_v2.json",
        },
    }


def _prepare_run(tmp_path: Path) -> ArtifactStore:
    (tmp_path / "data/case_demo/source").mkdir(parents=True)
    run_dir = tmp_path / "data/case_demo/runs/0123456789abcdef0123"
    return ArtifactStore(run_dir)


def test_workbench_renders_query_evidence_and_safe_source_links(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _prepare_run(tmp_path)
    store.write_json(
        "artifacts/query_plan_v1.json",
        {"archived": True},
    )
    store.write_json(
        "artifacts/query_plan_v2.json",
        {
            "plan_id": "query-plan:test",
            "intent": {
                "intent_id": "intent:test",
                "categories": ["regulatory"],
            },
            "queries": [
                {
                    "query_type": "company",
                    "category": "regulatory",
                    "query": "测试企业 监管处罚",
                }
            ],
            "added_queries": ["测试企业 监管处罚"],
            "removed_queries": [],
        },
    )
    store.write_json(
        "artifacts/research_result_v2.json",
        {
            "execution_status": "COMPLETE",
            "verification_status": "SUPPORTED",
            "candidate_count": 2,
            "verified_fact_count": 1,
            "company_result": {
                "status": "SUCCESS",
                "source": "snapshot",
                "verification_status": "SUPPORTED",
                "raw_result_count": 2,
                "rejected_result_count": 0,
                "content_fetch_status": "COMPLETE",
                "verification_execution_status": "COMPLETE",
                "facts": [],
                "evidence": [
                    {
                        "source_id": "safe-source",
                        "title": "监管公告 [可信]",
                        "source_url": "https://example.com/notice?id=1",
                        "source_tier": "A",
                        "evidence_stage": "VERIFIED",
                        "verification_status": "SUPPORTED",
                        "subject_match": "EXACT",
                        "category_match": True,
                        "relevance_score": 0.98,
                        "filter_reasons": [],
                        "verification_claim": {"statement": "测试企业受到监管处罚。"},
                        "verification": {
                            "relation": "SUPPORTS",
                            "accepted": True,
                            "confidence": 0.95,
                            "verifier_model": "rules-v1",
                            "evidence_location": "paragraph:3",
                            "evidence_excerpt": "监管机构作出处罚决定。",
                        },
                    },
                    {
                        "source_id": "unsafe-source",
                        "title": "不安全来源",
                        "source_url": "javascript:alert(1)",
                        "source_tier": "C",
                        "evidence_stage": "REJECTED",
                        "verification_status": "UNVERIFIED",
                        "subject_match": "NONE",
                        "category_match": False,
                        "relevance_score": 0.1,
                        "filter_reasons": ["SUBJECT_MISMATCH"],
                    },
                    {
                        "source_id": "private-source",
                        "title": "内网来源",
                        "source_url": "http://127.0.0.1/admin",
                        "source_tier": "C",
                        "evidence_stage": "REJECTED",
                        "verification_status": "UNVERIFIED",
                        "subject_match": "NONE",
                        "category_match": False,
                        "relevance_score": 0.0,
                        "filter_reasons": ["SUBJECT_MISMATCH"],
                    },
                ],
                "candidate_evidence": [],
            },
            "industry_result": {
                "status": "SUCCESS",
                "source": "not_requested",
                "verification_status": "NOT_FOUND",
                "facts": [],
                "evidence": [],
                "candidate_evidence": [],
            },
        },
    )

    details = load_workbench_details(_payload(), settings)
    markdown = workbench_detail_markdown(details)

    assert "测试企业 监管处罚" in markdown
    assert "`regulatory`\n\n| 类型 | 类别 | 确定性 Query |" in markdown
    assert "[打开来源](<https://example.com/notice?id=1>)" in markdown
    assert "javascript:" not in markdown
    assert "127.0.0.1" not in markdown
    assert "无安全可打开 URL" in markdown
    assert "paragraph:3" in markdown
    assert "监管机构作出处罚决定" in markdown
    assert "query_plan`：v1 → v2" in markdown


def test_workbench_trace_summarizes_retry_interrupt_and_resume(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _prepare_run(tmp_path)
    writer = TraceWriter(settings.trace_dir)
    now = datetime.now(UTC)
    for event_type, status in (
        ("RETRY", TraceStatus.RETRY),
        ("INTERRUPT", TraceStatus.SUCCESS),
        ("RESUME", TraceStatus.SUCCESS),
    ):
        writer.write(
            task_id="workbench-001",
            node="research" if event_type == "RETRY" else "runtime",
            event_type=event_type,
            status=status,
            start_time=now,
            end_time=now,
            latency_ms=7,
            input_summary="bounded summary",
        )

    details = load_workbench_details(_payload(), settings)
    markdown = workbench_detail_markdown(details)

    assert "Retry `1`" in markdown
    assert "Interrupt `1`" in markdown
    assert "Resume `1`" in markdown
    assert "Resume `1`\n\n| 时间 | 节点 | 事件 |" in markdown
    assert "bounded summary" in markdown


def test_workbench_rejects_trace_path_traversal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _prepare_run(tmp_path)
    payload = _payload()
    payload["thread_id"] = "../outside"

    details = load_workbench_details(payload, settings)

    assert details["trace_events"] == []
    assert "Trace 路径不安全，已拒绝读取" in details["warnings"]


def test_workbench_renders_financial_risk_and_safe_report_downloads(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _prepare_run(tmp_path)
    store.write_json(
        "artifacts/financial_analysis_v1.json",
        {
            "currency": "CNY_10K",
            "metrics": {
                "debt_ratio": {
                    "years": [2024, 2025],
                    "values": [0.4, 0.52],
                    "formula": "total_liabilities / total_assets",
                },
                "current_ratio": {
                    "years": [2024, 2025],
                    "values": [1.1, 0.9],
                    "formula": "current_assets / current_liabilities",
                },
            },
        },
    )
    store.write_json(
        "artifacts/risk_analysis_v1.json",
        {
            "risk_level": "HIGH",
            "summary": "短期偿债能力承压。",
            "risk_flags": [
                {
                    "severity": "HIGH",
                    "type": "liquidity",
                    "description": "流动比率低于 1。",
                    "evidence": ["metric:current_ratio"],
                }
            ],
        },
    )
    output_dir = store.run_dir / "output"
    output_dir.mkdir(parents=True)
    report = """# 企业报告

## 风险摘要

- 流动比率低于 1。

<script>alert('unsafe')</script>

[危险链接](javascript:alert(1))
"""
    (output_dir / "credit_report.md").write_text(report, encoding="utf-8")
    payload = _payload()
    payload["state"].update(
        {
            "financial_artifact": "artifacts/financial_analysis_v1.json",
            "risk_artifact": "artifacts/risk_analysis_v1.json",
            "query_plan_artifact": None,
            "research_artifact": None,
            "report_artifact": "output/credit_report.md",
            "anomaly_flags": ["DEBT_RATIO_RISING"],
        }
    )

    details = load_workbench_details(payload, settings)
    markdown = workbench_detail_markdown(details)

    assert "资产负债率" in markdown
    assert "2025：52.00%" in markdown
    assert "2025：0.90x" in markdown
    assert "DEBT_RATIO_RISING" in markdown
    assert "短期偿债能力承压" in markdown
    assert "报告预览与下载" in markdown
    assert "\\<script\\>" in markdown
    assert details["report_markdown"] == report
    assert "<script>" not in details["report_html"]
    assert "&lt;script&gt;" in details["report_html"]


def test_workbench_renders_citation_checked_llm_narrative(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _prepare_run(tmp_path)
    store.write_json(
        "artifacts/risk_narrative_v1.json",
        {
            "risk_level": "HIGH",
            "mode": "llm",
            "execution_status": "COMPLETE",
            "model_name": "qwen3.7-plus",
            "prompt_version": "m4a-risk-narrative-v1",
            "overall_summary": "流动性风险需要人工复核。",
            "summary_evidence_ids": ["metric:current_ratio"],
            "explanations": [
                {
                    "risk_id": "risk:1:liquidity",
                    "explanation": "流动比率低于一，短期偿债能力承压。",
                    "evidence_ids": ["metric:current_ratio"],
                }
            ],
            "limitations": ["不新增外部事实。"],
            "attempts": 1,
            "input_tokens": 100,
            "output_tokens": 50,
        },
    )
    payload = _payload()
    payload["state"].update(
        {
            "query_plan_artifact": None,
            "research_artifact": None,
            "risk_narrative_artifact": "artifacts/risk_narrative_v1.json",
        }
    )

    markdown = workbench_detail_markdown(load_workbench_details(payload, settings))

    assert "LLM 风险解释" in markdown
    assert "qwen3.7-plus" in markdown
    assert "risk:1:liquidity" in markdown
    assert "metric:current_ratio" in markdown
    assert "不新增外部事实" in markdown


def test_workbench_hides_raw_llm_degradation_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _prepare_run(tmp_path)
    store.write_json(
        "artifacts/risk_narrative_v1.json",
        {
            "risk_level": "HIGH",
            "mode": "llm",
            "execution_status": "DEGRADED",
            "model_name": "unconfigured",
            "prompt_version": "m4a-risk-narrative-v1",
            "overall_summary": "确定性风险摘要。",
            "summary_evidence_ids": ["metric:current_ratio"],
            "explanations": [
                {
                    "risk_id": "risk:1:liquidity",
                    "explanation": "流动比率低于一。",
                    "evidence_ids": ["metric:current_ratio"],
                }
            ],
            "limitations": ["当前内容使用确定性风险描述。"],
            "attempts": 0,
            "error_code": "CONFIG_ERROR",
            "error_message": "MODEL_API_KEY=top-secret is required",
        },
    )
    payload = _payload()
    payload["state"].update(
        {
            "query_plan_artifact": None,
            "research_artifact": None,
            "risk_narrative_artifact": "artifacts/risk_narrative_v1.json",
        }
    )

    markdown = workbench_detail_markdown(load_workbench_details(payload, settings))

    assert "已安全降级" in markdown
    assert "CONFIG_ERROR" in markdown
    assert "top-secret" not in markdown


def test_workbench_renders_bounded_evidence_summary_and_review_only_queries(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _prepare_run(tmp_path)
    evidence_id = "evidence:" + "a" * 64
    gap_id = "gap:" + "b" * 64
    store.write_json(
        "artifacts/evidence_summary_v1.json",
        {
            "mode": "llm",
            "execution_status": "COMPLETE",
            "model_name": "qwen3.7-plus",
            "prompt_version": "m4b-evidence-summary-query-proposal-v1",
            "verified_summary": "仅汇总已核验的债务风险事实。",
            "summary_evidence_ids": [evidence_id],
            "entries": [
                {
                    "evidence_id": evidence_id,
                    "source_id": "source-001",
                    "claim_id": "claim:" + "c" * 64,
                    "fact_id": "fact:" + "d" * 64,
                    "verification_status": "SUPPORTED",
                    "category": "debt",
                    "summary": "该证据已通过 Claim 级核验。",
                }
            ],
            "gaps": [
                {
                    "gap_id": gap_id,
                    "query_type": "industry",
                    "category": "regulatory",
                    "reason": "NO_CANDIDATE",
                    "summary": "行业监管类别暂无保留候选。",
                }
            ],
            "limitations": ["不将未核验证据写成事实。"],
            "attempts": 1,
            "input_tokens": 100,
            "output_tokens": 40,
        },
    )
    store.write_json(
        "artifacts/query_proposal_v1.json",
        {
            "mode": "llm",
            "execution_status": "DEGRADED",
            "model_name": "qwen3.7-plus",
            "prompt_version": "m4b-evidence-summary-query-proposal-v1",
            "allowed_categories": ["debt", "regulatory"],
            "proposals": [
                {
                    "query_type": "company",
                    "category": "debt",
                    "query": "测试企业 债务 展期",
                    "reference_ids": [evidence_id],
                    "rationale": "供人工补充调查。",
                    "decision": "ACCEPTED_FOR_REVIEW",
                    "rejection_reasons": [],
                },
                {
                    "query_type": "company",
                    "category": "fraud",
                    "query": "测试企业 舞弊",
                    "reference_ids": [evidence_id],
                    "rationale": "越权建议。",
                    "decision": "REJECTED",
                    "rejection_reasons": ["UNAUTHORIZED_CATEGORY"],
                },
            ],
            "limitations": ["规则 Query Plan 不会被修改。"],
            "attempts": 1,
            "error_code": "MODEL_ERROR",
            "error_message": "MODEL_API_KEY=top-secret unavailable",
        },
    )
    payload = _payload()
    payload["state"].update(
        {
            "query_plan_artifact": None,
            "research_artifact": None,
            "evidence_summary_artifact": "artifacts/evidence_summary_v1.json",
            "query_proposal_artifact": "artifacts/query_proposal_v1.json",
        }
    )

    markdown = workbench_detail_markdown(load_workbench_details(payload, settings))

    assert "Evidence Summary（受约束 LLM）" in markdown
    assert "Query Proposal（仅供人工审核）" in markdown
    assert evidence_id in markdown
    assert gap_id in markdown
    assert "ACCEPTED_FOR_REVIEW" in markdown
    assert "UNAUTHORIZED_CATEGORY" in markdown
    assert "不会自动执行" in markdown
    assert "MODEL_ERROR" in markdown
    assert "top-secret" not in markdown


def test_workbench_rejects_unsafe_report_reference(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _prepare_run(tmp_path)
    payload = _payload()
    payload["state"].update(
        {
            "query_plan_artifact": None,
            "research_artifact": None,
            "report_artifact": "output/../secret.md",
        }
    )

    details = load_workbench_details(payload, settings)

    assert details["report_markdown"] is None
    assert "report 无法读取：ValueError" in details["warnings"]


def test_workbench_classifies_common_provider_failures_and_redacts_keys() -> None:
    assert "配置缺失" in friendly_error_summary("TAVILY_API_KEY missing")
    assert "认证失败" in friendly_error_summary("HTTP 401 unauthorized")
    assert "额度不足" in friendly_error_summary("HTTP 429 quota exceeded")
    assert "响应超时" in friendly_error_summary("request timeout")
    assert "网络连接失败" in friendly_error_summary("DNS network error")
    assert "快照缺失" in friendly_error_summary("snapshot missing")
    redacted = friendly_error_summary("api_key=top-secret unexpected failure")
    assert "top-secret" not in redacted
    assert "[REDACTED]" in redacted
