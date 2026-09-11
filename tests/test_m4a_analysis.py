"""M4-A structured model gateway and cited main-chain narrative tests."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.llm.gateway import (
    ModelProgressEvent,
    OpenAICompatibleStructuredModel,
    StructuredModelError,
    StructuredModelResult,
    build_analysis_model,
)
from app.llm.risk_narrative import build_risk_narrative
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import mcp as research_mcp
from app.models.analysis import RiskNarrativeDraft
from app.models.risk import RiskAnalysis, RiskFlag, RiskLevel
from app.runtime.tasks import start_task
from app.runtime.tracing import read_trace

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        if kwargs.get("stream"):
            enable_thinking = (kwargs.get("extra_body") or {}).get("enable_thinking")
            chunks = []
            if enable_thinking:
                chunks.append(
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    reasoning_content="private reasoning must not leak",
                                )
                            )
                        ],
                        usage=None,
                    )
                )
            chunks.extend(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=content,
                                    reasoning_content=None,
                                )
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[],
                        usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
                    ),
                ]
            )
            return chunks
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
        )


class _FakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.completions = _FakeCompletions(contents)
        self.chat = SimpleNamespace(completions=self.completions)
        self.timeouts: list[float] = []

    def with_options(self, *, timeout: float) -> "_FakeClient":
        self.timeouts.append(timeout)
        return self


class _NarrativeModel:
    model_name = "mock-qwen"

    def __init__(self, *, unsupported: bool = False) -> None:
        self.unsupported = unsupported
        self.calls = 0

    def generate(self, *, output_schema: type, payload: dict, **_: Any):
        self.calls += 1
        explanations = []
        summary_evidence: list[str] = []
        for flag in payload["risk_flags"]:
            evidence = list(flag["allowed_evidence_ids"][:1])
            if self.unsupported:
                evidence = ["invented:evidence"]
            summary_evidence.extend(evidence)
            explanations.append(
                {
                    "risk_id": flag["risk_id"],
                    "explanation": f"受控解释：{flag['description']}",
                    "evidence_ids": evidence,
                }
            )
        output = output_schema.model_validate(
            {
                "overall_summary": "受控模型风险摘要。",
                "summary_evidence_ids": summary_evidence,
                "explanations": explanations,
                "limitations": ["只基于提供的风险证据。"],
            }
        )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=5,
            input_tokens=100,
            output_tokens=40,
        )


def _risk() -> RiskAnalysis:
    return RiskAnalysis(
        risk_level=RiskLevel.HIGH,
        risk_flags=[
            RiskFlag(
                type="liquidity",
                severity=RiskLevel.HIGH,
                description="期末流动比率低于 1。",
                evidence=["metric:current_ratio"],
            )
        ],
        requires_human_review=True,
        summary="短期偿债能力承压。",
    )


def test_openai_gateway_retries_invalid_json_and_returns_validated_output() -> None:
    valid = json.dumps(
        {
            "overall_summary": "风险较高。",
            "summary_evidence_ids": ["metric:current_ratio"],
            "explanations": [
                {
                    "risk_id": "risk:1:liquidity",
                    "explanation": "流动性承压。",
                    "evidence_ids": ["metric:current_ratio"],
                }
            ],
            "limitations": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(["not-json", valid])
    model = OpenAICompatibleStructuredModel(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=5,
        max_attempts=2,
        max_input_chars=10_000,
        max_output_tokens=500,
        client=client,
    )

    result = model.generate(
        output_schema=RiskNarrativeDraft,
        purpose="risk_narrative",
        prompt_version="test-v1",
        system_prompt="Return JSON only.",
        payload={"risk": "bounded"},
    )

    assert result.attempts == 2
    assert result.input_tokens == 123
    assert result.accounting_complete is False
    assert result.output.overall_summary == "风险较高。"
    assert len(client.completions.calls) == 2
    assert "tools" not in client.completions.calls[0]
    assert client.completions.calls[0]["stream"] is True
    assert "extra_body" not in client.completions.calls[0]
    assert client.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_openai_gateway_forwards_explicit_non_thinking_json_mode() -> None:
    content = json.dumps(
        {
            "overall_summary": "风险较高。",
            "summary_evidence_ids": ["metric:current_ratio"],
            "explanations": [
                {
                    "risk_id": "risk:1:liquidity",
                    "explanation": "流动性承压。",
                    "evidence_ids": ["metric:current_ratio"],
                }
            ],
            "limitations": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient([content])
    model = OpenAICompatibleStructuredModel(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=5,
        max_attempts=1,
        max_input_chars=10_000,
        max_output_tokens=500,
        enable_thinking=False,
        client=client,
    )

    result = model.generate(
        output_schema=RiskNarrativeDraft,
        purpose="risk_narrative",
        prompt_version="test-v1",
        system_prompt="Return JSON only.",
        payload={"risk": "bounded"},
        max_output_tokens=777,
    )

    assert client.completions.calls[0]["extra_body"] == {"enable_thinking": False}
    assert client.completions.calls[0]["max_tokens"] == 777
    assert result.accounting_complete is True


def test_openai_gateway_streams_private_thinking_then_strict_json() -> None:
    valid = json.dumps(
        {
            "overall_summary": "风险较高。",
            "summary_evidence_ids": ["metric:current_ratio"],
            "explanations": [
                {
                    "risk_id": "risk:1:liquidity",
                    "explanation": "流动性承压。",
                    "evidence_ids": ["metric:current_ratio"],
                }
            ],
            "limitations": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(["已核对风险项与允许引用。", valid])
    events: list[ModelProgressEvent] = []
    model = OpenAICompatibleStructuredModel(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=60,
        max_attempts=1,
        max_input_chars=10_000,
        max_output_tokens=500,
        enable_thinking=True,
        thinking_ttft_seconds=30,
        thinking_budget_tokens=200,
        process_summary_max_chars=100,
        client=client,
    )

    result = model.generate(
        output_schema=RiskNarrativeDraft,
        purpose="risk_narrative",
        prompt_version="test-v1",
        system_prompt="Return JSON only.",
        payload={"risk": "bounded"},
        progress_callback=events.append,
    )

    assert result.output.overall_summary == "风险较高。"
    assert result.accounting_complete is False
    assert len(client.completions.calls) == 2
    assert client.completions.calls[0]["extra_body"]["enable_thinking"] is True
    assert client.completions.calls[1]["extra_body"] == {"enable_thinking": False}
    assert "response_format" not in client.completions.calls[0]
    assert client.completions.calls[1]["response_format"] == {"type": "json_object"}
    assert client.timeouts == [30, 60]
    assert any(event.event_type == "LLM_PROCESS_SUMMARY" for event in events)
    assert any(event.event_type == "LLM_STRUCTURED_FIRST_TOKEN" for event in events)
    assert "private reasoning" not in json.dumps(
        [event.__dict__ for event in events], ensure_ascii=False
    )


def test_openai_gateway_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def build_client(**kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient([])

    monkeypatch.setattr("app.llm.gateway.OpenAI", build_client)

    OpenAICompatibleStructuredModel(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=17,
        max_attempts=2,
        max_input_chars=10_000,
        max_output_tokens=500,
    )

    assert captured["timeout"] == 17
    assert captured["max_retries"] == 0


def test_analysis_model_requires_configuration_without_exposing_key() -> None:
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="test-model",
        model_api_key="",
    )

    with pytest.raises(StructuredModelError) as captured:
        build_analysis_model(settings)

    assert captured.value.code == "CONFIG_ERROR"
    assert "MODEL_API_KEY" in str(captured.value)


def test_openai_gateway_exhausts_invalid_output_and_enforces_input_budget() -> None:
    client = _FakeClient(["not-json", "still-not-json"])
    model = OpenAICompatibleStructuredModel(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=5,
        max_attempts=2,
        max_input_chars=1_000,
        max_output_tokens=500,
        client=client,
    )

    with pytest.raises(StructuredModelError) as invalid:
        model.generate(
            output_schema=RiskNarrativeDraft,
            purpose="risk_narrative",
            prompt_version="test-v1",
            system_prompt="Return JSON only.",
            payload={"risk": "bounded"},
        )
    assert invalid.value.code == "INVALID_OUTPUT"
    assert invalid.value.attempts == 2

    with pytest.raises(StructuredModelError) as oversized:
        model.generate(
            output_schema=RiskNarrativeDraft,
            purpose="risk_narrative",
            prompt_version="test-v1",
            system_prompt="Return JSON only.",
            payload={"risk": "x" * 2_000},
        )
    assert oversized.value.code == "INPUT_TOO_LARGE"
    assert oversized.value.attempts == 0


def test_risk_narrative_rejects_invented_evidence_and_degrades() -> None:
    model = _NarrativeModel(unsupported=True)

    narrative = build_risk_narrative(
        _risk(),
        analysis_mode="llm",
        model=model,
        model_name=model.model_name,
    )

    assert model.calls == 1
    assert narrative.execution_status == "DEGRADED"
    assert narrative.error_code == "UNSUPPORTED_EVIDENCE"
    assert narrative.overall_summary == "短期偿债能力承压。"
    assert narrative.explanations[0].evidence_ids == ["metric:current_ratio"]


def test_deterministic_narrative_never_calls_model() -> None:
    model = _NarrativeModel()

    narrative = build_risk_narrative(
        _risk(),
        analysis_mode="deterministic",
        model=model,
        model_name=model.model_name,
    )

    assert model.calls == 0
    assert narrative.execution_status == "NOT_REQUESTED"
    assert narrative.mode == "deterministic"


def test_llm_risk_narrative_enters_durable_main_chain_without_changing_hitl(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        data_dir / "case_risky/source",
    )
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="mock-qwen",
        model_api_key="test-key",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    model = _NarrativeModel()

    payload = start_task(
        thread_id="m4a-main-chain",
        case_id="case_risky",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )

    assert payload["state"]["status"] == "WAITING_APPROVAL"
    assert payload["next"] == ["approval"]
    assert payload["interrupts"]
    assert payload["state"]["risk_level"] == "HIGH"
    assert payload["state"]["risk_narrative_artifact"] == (
        "artifacts/risk_narrative_v1.json"
    )
    run_dir = data_dir / "case_risky/runs" / payload["state"]["run_id"]
    narrative = json.loads(
        (run_dir / "artifacts/risk_narrative_v1.json").read_text(encoding="utf-8")
    )
    assert narrative["execution_status"] == "COMPLETE"
    assert narrative["model_name"] == "mock-qwen"
    assert narrative["explanations"]
    events = read_trace(tmp_path / "traces/m4a-main-chain.jsonl")
    llm_events = [
        event
        for event in events
        if event["event_type"] == "LLM_CALL"
        and event["input_summary"] == "purpose=risk_narrative"
    ]
    assert len(llm_events) == 1
    assert "execution=COMPLETE" in llm_events[0]["output_summary"]
    serialized_trace = json.dumps(events, ensure_ascii=False)
    assert "test-key" not in serialized_trace
    assert "受控模型风险摘要" not in serialized_trace


def test_missing_llm_configuration_degrades_without_breaking_main_chain(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        data_dir / "case_risky/source",
    )
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="mock-qwen",
        model_api_key="",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )

    payload = start_task(
        thread_id="m4a-missing-config",
        case_id="case_risky",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
    )

    assert payload["state"]["status"] == "WAITING_APPROVAL"
    run_dir = data_dir / "case_risky/runs" / payload["state"]["run_id"]
    narrative = json.loads(
        (run_dir / "artifacts/risk_narrative_v1.json").read_text(encoding="utf-8")
    )
    assert narrative["execution_status"] == "DEGRADED"
    assert narrative["error_code"] == "CONFIG_ERROR"
    assert narrative["explanations"]
    events = read_trace(tmp_path / "traces/m4a-missing-config.jsonl")
    llm_event = next(event for event in events if event["event_type"] == "LLM_CALL")
    assert llm_event["status"] == "FAILED"
    assert llm_event["error"] == "CONFIG_ERROR"
