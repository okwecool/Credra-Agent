"""Offline gateway accounting for PROCESS plus mandatory structured JSON."""

import pytest
from pydantic import BaseModel

from app.llm.gateway import OpenAICompatibleStructuredModel, StructuredModelError
from credra_agent.execution.model_budget import model_reservation
from tests.test_m4a_analysis import _FakeClient
from tests.test_v2_evidence_replanning import caps


class Output(BaseModel):
    value: int


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
