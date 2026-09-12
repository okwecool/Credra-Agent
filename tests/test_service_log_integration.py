"""Production collector, actual MCP, model statuses and durable logging pause."""

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm.gateway import OpenAICompatibleStructuredModel, StructuredModelError
from app.mcp.research_client import ResearchMCPClient
from credra_agent.observability.collector import (
    Collector,
    LogConfig,
    LoggingUnavailable,
    send,
)
from credra_agent.observability.events import event_record, log_context, reference_id
from credra_agent.observability.runtime import (
    service_session,
    start_process_service,
)

ROOT = Path(__file__).resolve().parents[1]


def read_events(directory):
    return [
        json.loads(line)
        for path in sorted(directory.glob("service.*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_real_mcp_children_and_restart_join_one_batch(tmp_path):
    with service_session("test", LogConfig(tmp_path, max_bytes=4096)) as service:

        async def task(identity):
            with log_context(thread_id=identity, run_id=identity, node="research"):
                client = ResearchMCPClient(
                    environment={
                        "RESEARCH_PROVIDER": "mock",
                        "CONTENT_FETCH_PROVIDER": "disabled",
                        "FACT_VERIFIER": "rules",
                    }
                )
                return await client.search_company("迅驰供应链科技有限公司")

        results = await asyncio.gather(task("a"), task("b"))
        await task("a")
        assert all(result.found for result in results)
        directory = service.collector.writer.directory
        assert service.healthy and service.collector.healthy
    events = read_events(directory)
    child_events = [e for e in events if e["service"] == "research_mcp"]
    assert len({e["process_instance_id"] for e in child_events}) == 3
    assert {e["startup_id"] for e in events} == {directory.name}
    assert {
        e.get("thread_id") for e in child_events if e["event_type"] == "SOURCE_RESULT"
    } >= {reference_id("a"), reference_id("b")}
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert all(p.stat().st_size <= 4096 for p in directory.glob("service.*.jsonl"))
    assert json.loads((directory / "startup.json").read_text())["normal_shutdown"]


def test_collector_deduplicates_and_rejects_untrusted_wire_fields(tmp_path):
    collector = Collector(LogConfig(tmp_path))
    try:
        event = event_record("NODE_START", service="test")
        assert send(collector.endpoint, event) == send(collector.endpoint, event)
        with pytest.raises(LoggingUnavailable):
            send({**collector.endpoint, "token": "wrong"}, event)
        with pytest.raises(LoggingUnavailable):
            send(collector.endpoint, {**event, "prompt": "must-not-be-written"})
        assert collector.healthy
    finally:
        collector.close()
    assert len(read_events(collector.writer.directory)) == 1


def test_queue_congestion_pauses_with_bounded_wait_and_drain(tmp_path, monkeypatch):
    collector = Collector(
        LogConfig(
            tmp_path,
            queue_capacity=1,
            enqueue_timeout=0.05,
            ack_timeout=0.2,
            shutdown_timeout=0.2,
        )
    )
    entered, release = threading.Event(), threading.Event()
    append = collector.writer.append

    def slow(record):
        entered.set()
        assert release.wait(2)
        return append(record)

    monkeypatch.setattr(collector.writer, "append", slow)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(
            collector.publish, event_record("NODE_START", service="test")
        )
        assert entered.wait(1)
        second = pool.submit(
            collector.publish, event_record("NODE_START", service="test")
        )
        # A third producer cannot enter the single-slot queue while the writer
        # holds the first record. ACK deadlines are also bounded.
        third = pool.submit(
            collector.publish, event_record("NODE_START", service="test")
        )
        for future in (second, third, first):
            with pytest.raises(LoggingUnavailable):
                future.result(timeout=1)
        release.set()
    collector.close()
    assert not collector.healthy
    assert not json.loads((collector.writer.directory / "startup.json").read_text())[
        "normal_shutdown"
    ]


class Output(BaseModel):
    value: int


class FakeModel:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        item = next(self.outputs)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return iter(
            [
                SimpleNamespace(
                    id="provider-request",
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=item, reasoning_content=None),
                            finish_reason="stop",
                        )
                    ],
                )
            ]
        )


def model(outputs, **options):
    return OpenAICompatibleStructuredModel(
        api_key="test-placeholder",
        base_url="https://example.invalid",
        model_name="test-model",
        timeout_seconds=1,
        max_attempts=len(outputs),
        max_input_chars=10000,
        max_output_tokens=100,
        client=FakeModel(outputs),
        **options,
    )


def generate(instance):
    return instance.generate(
        output_schema=Output,
        purpose="test",
        prompt_version="v1",
        system_prompt="not logged",
        payload={"private": "secret-sentinel"},
    )


def test_llm_transport_json_schema_retries_and_unknown_usage_are_distinct(tmp_path):
    with service_session("test", LogConfig(tmp_path)) as service:
        result = generate(model(["{", '{"value":"invalid"}', '{"value":3}']))
        assert result.output.value == 3 and result.attempts == 3
        directory = service.collector.writer.directory
    events = read_events(directory)
    ends = [e for e in events if e["event_type"] == "LLM_ATTEMPT_END"]
    assert len(ends) == 3 and all(e["status"] == "SUCCESS" for e in ends)
    assert len({e["call_id"] for e in ends}) == 1
    assert all(e["input_tokens"] is None and e["http_status"] is None for e in ends)
    assert {e["status"] for e in events if e["event_type"] == "LLM_VALIDATION"} >= {
        "INVALID_JSON",
        "INVALID_SCHEMA",
        "SUCCESS",
    }
    assert "secret-sentinel" not in json.dumps(events)


def test_llm_timeout_and_sdk_sensitive_exception_are_safe(tmp_path, capsys):
    service = start_process_service("test", LogConfig(tmp_path))
    try:
        with pytest.raises(StructuredModelError):
            generate(model([TimeoutError("secret-sentinel")]))
        try:
            raise ValueError("secret-sentinel")
        except ValueError:
            logging.getLogger("httpx").exception(
                "Authorization: secret-sentinel https://example.invalid/?key=secret-sentinel"
            )
        directory = service.collector.writer.directory
    finally:
        service.stop_process()
    events = read_events(directory)
    assert any(e.get("status") == "TIMEOUT" for e in events)
    assert any(e["event_type"] == "SDK_DIAGNOSTIC" and e.get("stack") for e in events)
    assert "secret-sentinel" not in json.dumps(events) + capsys.readouterr().err


def test_checkpoint_resume_after_logging_failure_skips_committed_node(tmp_path):
    case = tmp_path / "data/case_saic_600104/source"
    shutil.copytree(ROOT / "data/case_saic_600104/source", case)
    code = """
import json,sys
from pathlib import Path
from app.config import Settings
from app.runtime.tasks import start_task,resume_task
from credra_agent.observability.runtime import current
import app.graph.workflow as workflow
root=Path(sys.argv[1]); mode=sys.argv[2]
settings=Settings(_env_file=None,analysis_mode="deterministic",model_api_key="",research_provider="mock",content_fetch_provider="disabled",fact_verifier="rules",data_dir=root/"data",checkpoint_db_path=root/"checkpoints.sqlite",trace_dir=root/"traces",service_log_dir=root/"logs")
if mode=="pause":
 original=workflow.emit
 def fail_after_node(kind,**fields):
  result=original(kind,**fields)
  if kind=="NODE_END": current().collector.fail()
  return result
 workflow.emit=fail_after_node
 result=start_task(thread_id="test-resume",case_id="case_saic_600104",settings=settings)
else:
 def must_not_repeat(*args): raise AssertionError("document repeated")
 workflow.normalize_company_documents=must_not_repeat
 result=resume_task(thread_id="test-resume",decision="resume_logging",comment=None,settings=settings)
print(json.dumps(result,ensure_ascii=True))
"""

    def process(mode):
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), mode],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    paused = process("pause")
    assert paused["execution_blocked"]["resume_safe"]
    assert (
        paused["state"]["company_artifact"]
        and not paused["state"]["financial_artifact"]
    )
    resumed = process("resume")
    assert "execution_blocked" not in resumed
    assert resumed["state"]["status"] == "WAITING_APPROVAL"
    assert resumed["state"]["company_artifact"] == paused["state"]["company_artifact"]
    assert len(list((tmp_path / "logs").iterdir())) == 2


def test_unpaused_task_cannot_bypass_review_using_logging_resume(tmp_path):
    from app.runtime.tasks import resume_task, start_task

    shutil.copytree(
        ROOT / "data/case_saic_600104/source", tmp_path / "data/case_saic_600104/source"
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        checkpoint_db_path=tmp_path / "db.sqlite",
        trace_dir=tmp_path / "traces",
    )
    start_task(thread_id="review", case_id="case_saic_600104", settings=settings)
    with pytest.raises(ValueError, match="not paused"):
        resume_task(
            thread_id="review",
            decision="resume_logging",
            comment=None,
            settings=settings,
        )


def test_shutdown_deadline_does_not_report_normal_while_writer_is_stuck(
    tmp_path, monkeypatch
):
    collector = Collector(LogConfig(tmp_path, ack_timeout=0.2, shutdown_timeout=0.1))
    entered, release = threading.Event(), threading.Event()
    original = collector.writer.append

    def blocked(event):
        entered.set()
        assert release.wait(2)
        return original(event)

    monkeypatch.setattr(collector.writer, "append", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            collector.publish, event_record("NODE_START", service="test")
        )
        assert entered.wait(1)
        start = monotonic()
        collector.close()
        assert monotonic() - start < 0.8
        assert not json.loads(
            (collector.writer.directory / "startup.json").read_text()
        )["normal_shutdown"]
        release.set()
        collector.worker.join(1)
        # The already-started write may succeed after close timed out; its bytes
        # remain valid, but the startup still discloses incomplete shutdown.
        try:
            future.result(timeout=1)
        except LoggingUnavailable:
            pass
    assert not json.loads((collector.writer.directory / "startup.json").read_text())[
        "normal_shutdown"
    ]


def test_cli_json_is_clean_and_invalid_configuration_is_redacted(tmp_path):
    import os

    shutil.copytree(
        ROOT / "data/case_normal/source", tmp_path / "data/case_normal/source"
    )
    environment = {
        **os.environ,
        "SERVICE_LOG_DIR": str(tmp_path / "logs"),
        "TRACE_DIR": str(tmp_path / "traces"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.task_cli",
            "--data-dir",
            str(tmp_path / "data"),
            "--db",
            str(tmp_path / "db.sqlite"),
            "start",
            "--thread-id",
            "json-output",
            "--case-id",
            "case_normal",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"]["status"] == "COMPLETED"
    environment["SERVICE_LOG_ACK_TIMEOUT"] = "secret-sentinel-invalid"
    result = subprocess.run(
        [sys.executable, "-m", "app.task_cli", "status", "--thread-id", "x"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "CONFIGURATION_INVALID"}
    assert "secret-sentinel" not in result.stdout + result.stderr
    all_logs = [
        p.read_text(encoding="utf-8") for p in (tmp_path / "logs").rglob("*.jsonl")
    ]
    assert all_logs and "secret-sentinel" not in "".join(all_logs)


def test_chainlit_pause_exposes_only_safe_resume_and_refresh(monkeypatch):
    import app.chainlit_app as ui

    monkeypatch.setattr(ui.cl, "Action", lambda **kwargs: SimpleNamespace(**kwargs))
    actions = ui._task_actions(
        {
            "thread_id": "paused",
            "execution_blocked": {"resume_safe": True},
            "interrupts": [{}],
        }
    )
    assert {action.name for action in actions} == {"resume_logging", "refresh_task"}


def test_child_logging_gap_preserves_tool_result_and_blocks_parent(tmp_path):
    from app.mcp.research_server import _search
    from credra_agent.observability.runtime import require_logging

    payload = _search(
        "company", "迅驰供应链科技有限公司", settings=Settings(_env_file=None)
    ).model_dump(mode="json")
    with service_session("test", LogConfig(tmp_path)) as service:
        result = ResearchMCPClient._parse_result(
            SimpleNamespace(
                structured_content={**payload, "_service_log_available": False}
            )
        )
        assert result.found
        assert not service.healthy and not service.collector.healthy
        with pytest.raises(LoggingUnavailable, match="BEFORE_NODE"):
            require_logging()


@pytest.mark.parametrize("failure", ["http429", "stream"])
def test_llm_rate_limit_and_broken_stream_have_terminal_attempts(tmp_path, failure):
    import httpx
    from openai import OpenAIError, RateLimitError

    if failure == "http429":
        item = RateLimitError(
            "secret-sentinel",
            response=httpx.Response(
                429, request=httpx.Request("POST", "https://example.invalid")
            ),
            body=None,
        )
    else:

        def broken():
            yield SimpleNamespace(
                id="provider-request",
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="secret-sentinel", reasoning_content=None
                        ),
                        finish_reason=None,
                    )
                ],
            )
            raise OpenAIError("secret-sentinel")

        item = broken
    with service_session("test", LogConfig(tmp_path)) as service:
        with pytest.raises(StructuredModelError):
            generate(model([item]))
        directory = service.collector.writer.directory
    events = read_events(directory)
    ends = [e for e in events if e["event_type"] == "LLM_ATTEMPT_END"]
    assert len(ends) == 1
    assert ends[0]["status"] == ("RATE_LIMITED" if failure == "http429" else "FAILED")
    if failure == "http429":
        assert ends[0]["http_status"] == 429
    else:
        assert any(e["event_type"] == "LLM_FIRST_TOKEN" for e in events)
    assert "secret-sentinel" not in json.dumps(events)


def test_process_phase_and_actual_citation_rejection_degrade_separately(tmp_path):
    from app.llm.risk_narrative import build_risk_narrative
    from app.tools.artifacts import ArtifactStore
    from tests.test_m4a_analysis import _NarrativeModel, _risk

    with service_session("test", LogConfig(tmp_path / "logs")) as service:
        assert (
            generate(
                model(["public summary", '{"value":3}'], enable_thinking=True)
            ).output.value
            == 3
        )
        fake = _NarrativeModel(unsupported=True)
        narrative = build_risk_narrative(
            _risk(), analysis_mode="llm", model=fake, model_name=fake.model_name
        )
        assert narrative.execution_status == "DEGRADED"
        ArtifactStore(tmp_path / "run").write_json("artifacts/risk_v1.json", narrative)
        directory = service.collector.writer.directory
    events = read_events(directory)
    ends = [e for e in events if e["event_type"] == "LLM_ATTEMPT_END"]
    assert {e["phase"] for e in ends} == {"PROCESS", "STRUCTURED"}
    assert len({e["call_id"] for e in ends}) == 1
    assert any(e.get("status") == "INVALID_CITATION" for e in events)
    assert any(e["event_type"] == "DEGRADED" for e in events)


def test_concurrent_root_archives_and_abrupt_process_exit(tmp_path):
    def root(_):
        collector = Collector(LogConfig(tmp_path))
        collector.publish(event_record("SERVICE_START", service="test"))
        collector.close()
        return collector.writer.directory

    with ThreadPoolExecutor(max_workers=2) as pool:
        directories = list(pool.map(root, range(2)))
    assert len(set(directories)) == 2
    code = "import os,sys; from pathlib import Path; from credra_agent.observability.collector import LogConfig; from credra_agent.observability.runtime import start_process_service,emit; start_process_service('test',LogConfig(Path(sys.argv[1]))); emit('NODE_START',thread_id='interrupted'); os._exit(7)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 7
    abandoned = next(p for p in tmp_path.iterdir() if p not in directories)
    assert not json.loads((abandoned / "startup.json").read_text())["normal_shutdown"]
    assert any(
        e.get("thread_id") == reference_id("interrupted")
        for e in read_events(abandoned)
    )


def test_http_request_id_is_not_invented_from_completion_id(tmp_path):
    class ResponseStream:
        response = SimpleNamespace(
            status_code=200, headers={"x-request-id": "actual-request-id"}
        )

        def __iter__(self):
            yield SimpleNamespace(
                id="completion-id",
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content='{"value":4}', reasoning_content=None
                        ),
                        finish_reason="stop",
                    )
                ],
            )

    with service_session("test", LogConfig(tmp_path)) as service:
        assert generate(model([ResponseStream])).output.value == 4
        directory = service.collector.writer.directory
    end = next(
        e for e in read_events(directory) if e["event_type"] == "LLM_ATTEMPT_END"
    )
    assert end["http_status"] == 200 and end["finish_reason"] == "stop"
    assert end["request_id"] == reference_id("actual-request-id")
    assert end["request_id"] != reference_id("completion-id")
