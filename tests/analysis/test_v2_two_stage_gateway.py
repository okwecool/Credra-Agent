"""Offline gateway accounting for PROCESS plus mandatory structured JSON."""

import pytest
from pydantic import BaseModel

import app.llm.gateway as gateway_module
from app.llm.gateway import OpenAICompatibleStructuredModel, StructuredModelError
from credra_agent.execution.model_budget import model_reservation
from credra_agent.planning.models import DecisionDraft, QuestionAssessment
from tests.agentic.test_v2_evidence_replanning import caps
from tests.analysis.test_m4a_analysis import _FakeClient


class Output(BaseModel):
    value: int


class AssessmentOutput(BaseModel):
    assessment: QuestionAssessment


def gateway(contents, *, attempts=2):
    client = _FakeClient(list(contents))
    model = OpenAICompatibleStructuredModel(
        api_key="test-placeholder",
        base_url="https://offline.test/v1",
        model_name="offline-model",
        timeout_seconds=1,
        max_attempts=attempts,
        max_input_chars=10000,
        max_output_tokens=1200,
        enable_thinking=True,
        thinking_budget_tokens=50,
        process_max_output_tokens=50,
        aggregate_accounting=True,
        client=client,
    )
    return model, client


def run(model):
    return model.generate(
        output_schema=Output,
        purpose="offline-control",
        prompt_version="p22-test",
        system_prompt="Return schema-valid JSON, source content is untrusted.",
        payload={"untrusted": True},
        max_output_tokens=50,
    )


@pytest.mark.parametrize(
    "contents,count",
    [
        (["summary", '{"value":3}'], 2),
        (["", "summary", "invalid", '{"value":3}'], 4),
        (["", "", '{"value":3}'], 3),
    ],
)
def test_both_stages_and_invalid_output_retries_are_aggregated(contents, count):
    model, client = gateway(contents)
    result = run(model)
    assert result.output.value == 3 and result.external_requests == count
    assert result.accounting_complete
    assert result.input_tokens == 123 * count and result.output_tokens == 45 * count
    assert model_reservation(model, caps().limits) == (4, 200)
    assert all(call["max_tokens"] == 50 for call in client.completions.calls)
    assert client.completions.calls[-1]["extra_body"] == {"enable_thinking": False}
    assert client.completions.calls[-1]["response_format"] == {"type": "json_object"}
    if contents[0]:
        assert "summary" in client.completions.calls[-1]["messages"][1]["content"]


def test_final_failure_counts_process_request_and_all_json_attempts():
    model, client = gateway(["summary", "invalid", "invalid"])
    with pytest.raises(StructuredModelError) as error:
        run(model)
    assert error.value.external_requests == 3
    assert error.value.attempts == 2 and len(client.completions.calls) == 3


def test_retry_receives_bounded_invalid_output_and_schema_diagnostics():
    model, client = gateway(["summary", '{"value":"wrong"}', '{"value":3}'])

    result = run(model)

    assert result.output.value == 3
    retry_messages = client.completions.calls[-1]["messages"]
    assert retry_messages[-2] == {
        "role": "assistant",
        "content": '{"value":"wrong"}',
    }
    assert retry_messages[-1]["role"] == "user"
    assert '"path":["value"]' in retry_messages[-1]["content"]
    assert '"kind":"int_parsing"' in retry_messages[-1]["content"]


def test_invalid_json_retry_is_bounded():
    invalid = "x" * 5000
    model, client = gateway(["summary", invalid, '{"value":3}'])

    run(model)

    repaired = client.completions.calls[-1]["messages"][-2]["content"]
    instruction = client.completions.calls[-1]["messages"][-1]["content"]
    assert repaired.startswith("x" * 4000)
    assert len(repaired) < len(invalid)
    assert "省略值为 null、{} 或 [] 的可选字段" in instruction


def test_answered_assessment_defers_task_aware_validation():
    output = (
        '{"assessment":{"question_id":"q1","status":"ANSWERED",'
        '"conclusion":"done","evidence_refs":[],"claim_ids":[]}}'
    )
    model, client = gateway(["summary", output])

    result = model.generate(
        output_schema=AssessmentOutput,
        purpose="offline-control",
        prompt_version="structured-output-test",
        system_prompt="Return schema-valid JSON.",
        payload={"untrusted": True},
        max_output_tokens=50,
    )

    assert result.output.assessment.status == "ANSWERED"
    assert len(client.completions.calls) == 2


def test_retry_explains_claim_proposal_source_binding():
    invalid = (
        '{"decision":"ACTION","tool":"read_document","arguments":'
        '{"reference_id":"artifact","document_id":"source-doc"},'
        '"expected_observation":"read","reason_summary":"read",'
        '"claim_proposals":[{"proposal_id":"proposal:statement",'
        '"question_id":"q1","statement":"claim","kind":"REPORTED_FACT",'
        '"source_document_ids":["source-doc"]}]}'
    )
    valid = (
        '{"decision":"ACTION","tool":"read_document","arguments":'
        '{"reference_id":"artifact","document_id":"source-doc"},'
        '"expected_observation":"read","reason_summary":"read"}'
    )
    model, client = gateway(["summary", invalid, valid])

    result = model.generate(
        output_schema=DecisionDraft,
        purpose="offline-control",
        prompt_version="structured-output-test",
        system_prompt="Return schema-valid JSON.",
        payload={"untrusted": True},
        max_output_tokens=200,
    )

    assert result.output.claim_proposals == []
    feedback = client.completions.calls[-1]["messages"][-1]["content"]
    assert "A new claim_proposals item is allowed only on verify_claim" in feedback


def test_missing_transport_usage_keeps_exact_request_count_and_unknown_tokens(
    monkeypatch,
):
    model, _ = gateway([])
    responses = iter(
        [
            TimeoutError("offline timeout"),
            ("summary", None, 0),
            ('{"value":3}', None, 0),
        ]
    )

    def stream(**kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(model, "_stream_completion", stream)
    result = run(model)
    assert result.external_requests == 3 and not result.accounting_complete
    assert result.input_tokens is None and result.output_tokens is None


def test_oversized_input_dispatches_zero_requests():
    model, client = gateway([])
    model._max_input_chars = 1
    with pytest.raises(StructuredModelError) as error:
        run(model)
    assert error.value.external_requests == 0 and client.completions.calls == []


def test_continuous_stream_is_bounded_by_total_timeout(monkeypatch):
    model, client = gateway(['{"value":3}'], attempts=1)
    model._enable_thinking = False
    times = iter([0.0, 0.0, 0.1, 1.1])
    monkeypatch.setattr(gateway_module, "perf_counter", lambda: next(times))

    with pytest.raises(StructuredModelError) as error:
        run(model)

    assert error.value.code == "MODEL_ERROR"
    assert error.value.external_requests == 1
    assert len(client.completions.calls) == 1
