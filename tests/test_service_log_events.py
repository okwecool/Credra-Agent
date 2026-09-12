"""Service event confidentiality and concurrent task context boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from credra_agent.observability.events import event_record, log_context, reference_id


def test_service_events_never_serialize_free_form_provider_or_user_payloads():
    sentinel = "secret-sentinel-DO-NOT-STORE"
    try:
        raise RuntimeError(sentinel)
    except RuntimeError as exc:
        event = event_record(
            "LLM_ATTEMPT_END",
            service="gateway",
            exception=exc,
            status="FAILED",
            phase="STRUCTURED",
            attempt=2,
            thread_id=sentinel,
            request_id=sentinel,
            model=sentinel,
            message=sentinel,
            prompt=sentinel,
            reasoning_content=sentinel,
            api_key=sentinel,
            authorization=sentinel,
            url="https://example.invalid/?api_key=" + sentinel,
            input_tokens=None,
            output_tokens=0,
        )
    assert sentinel not in json.dumps(event)
    assert event["thread_id"] == reference_id(sentinel)
    assert event["input_tokens"] is None and event["output_tokens"] == 0
    assert event["status"] == "FAILED" and event["stack"]


@pytest.mark.asyncio
async def test_nested_async_and_worker_context_do_not_mix_tasks():
    async def operation(task):
        with log_context(thread_id=task):
            with log_context(node="financial"):
                await asyncio.sleep(0)
                nested = await asyncio.to_thread(
                    event_record, "NODE_START", service="graph"
                )
            outer = event_record("NODE_END", service="graph")
        return nested, outer

    results = await asyncio.gather(operation("a"), operation("b"))
    for task, (nested, outer) in zip(("a", "b"), results, strict=True):
        assert nested["thread_id"] == outer["thread_id"] == reference_id(task)
        assert nested["node"] == "financial" and "node" not in outer
    assert "thread_id" not in event_record("SERVICE_START", service="runtime")


def test_explicit_thread_context_and_invalid_event_rejection():
    def operation(task):
        with log_context(thread_id=task):
            return event_record("NODE_START", service="graph")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operation, ("a", "b")))
    assert {r["thread_id"] for r in results} == {reference_id("a"), reference_id("b")}
    with pytest.raises(ValueError):
        event_record("MODEL_GENERATED_EVENT", service="graph")
