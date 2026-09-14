"""P26-4 OS-process crashes, actual locks and durable entry/task accounting."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from credra_agent.entry.budget import EntryBudgetStore
from credra_agent.entry.context import workspace_reference
from credra_agent.entry.store import EntryStore
from tests.entry_recovery_cli import CONVERSATION, settings_for
from tests.test_v2_entry_delegation import CONTROLS
from tests.test_v2_entry_dialogue import policy_data
from tests.test_v2_ui_execution import policy_dict

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def directory(tmp_path):
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            ROOT / "data" / case / "source", tmp_path / "data" / case / "source"
        )
    (tmp_path / "entry.json").write_text(
        json.dumps(policy_data(allowed_control_tools=CONTROLS)), encoding="utf-8"
    )
    (tmp_path / "task.json").write_text(json.dumps(policy_dict()), encoding="utf-8")
    store = EntryStore(tmp_path / "tasks.db")
    store.ensure_conversation(CONVERSATION, workspace_reference(settings_for(tmp_path)))
    return tmp_path


def run(directory, command, boundary="", expected=0):
    environment = os.environ.copy()
    environment.pop("CREDRA_LOG_ENDPOINT", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.entry_recovery_cli",
            str(directory),
            command,
            "--boundary",
            boundary,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert "CREDRA_LOG_UNAVAILABLE" not in result.stderr, result.stderr
    return json.loads(result.stdout.splitlines()[-1]) if expected == 0 else None


def counts(directory):
    entry = (
        [
            json.loads(line)["kind"]
            for line in (directory / "entry-calls.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        if (directory / "entry-calls.jsonl").exists()
        else []
    )
    task = (
        [
            json.loads(line)["purpose"]
            for line in (directory / "calls.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        if (directory / "calls.jsonl").exists()
        else []
    )
    return entry.count("entry"), entry.count("tool"), len(task)


@pytest.mark.parametrize(
    "boundary",
    [
        "context_saved",
        "model_saved",
        "command_reserved",
        "intent_saved",
        "command_queued",
    ],
)
def test_known_undispatched_windows_resume_without_repaying_entry(directory, boundary):
    run(directory, "start", boundary, 73)
    before = counts(directory)
    if boundary == "command_queued":
        recovered = run(directory, "recover")
        assert recovered["selected"] and len(recovered["tasks"]["items"]) == 1
    replay = run(directory, "replay")
    assert replay["result"]["status"] == "REPLY"
    assert counts(directory) == (1, 1, 2)
    assert replay["selected"] and len(replay["tasks"]["items"]) == 1
    assert replay["result"]["budget"]["external_spent"] == 1
    assert before[0] == (0 if boundary == "context_saved" else 1)
    again = run(directory, "replay")
    assert again["result"]["duplicate"] and counts(directory) == (1, 1, 2)


def test_returned_but_unstored_model_is_never_reissued(directory):
    run(directory, "start", "model_returned", 73)
    replay = run(directory, "replay")
    assert replay["result"]["status"] == "LIMITED"
    assert "ENTRY_MODEL_RESULT_UNCERTAIN" in replay["result"]["limitations"]
    assert replay["result"]["budget"]["external_spent"] == 2
    assert replay["result"]["budget"]["token_spent"] == 5000
    assert counts(directory) == (1, 0, 0) and not replay["tasks"]["items"]
    hello = run(directory, "hello")
    assert hello["result"]["status"] == "LIMITED" and counts(directory) == (1, 0, 0)


@pytest.mark.parametrize(
    "boundary",
    ["command_dispatched", "graph_finished", "after_request", "after_result_stored"],
)
def test_dispatched_windows_are_unknown_and_only_explicit_resume_uses_original_task(
    directory, boundary
):
    run(directory, "start", boundary, 73)
    before = counts(directory)
    recovered = run(directory, "recover")
    store = EntryStore(directory / "tasks.db")
    command = store.latest_command(recovered["selected"])
    assert command["status"] == "UNCERTAIN" and counts(directory) == before
    replay = run(directory, "replay")
    assert replay["result"]["status"] == "LIMITED" and counts(directory) == before
    resumed = run(directory, "resume")
    assert (
        resumed["selected"] == recovered["selected"]
        and len(resumed["tasks"]["items"]) == 1
    )
    after = counts(directory)
    assert after[0] == before[0] + 1 and after[1] <= 1
    if boundary == "after_request":
        assert (
            after[1] == before[1]
        )  # Provider returned, no durable result: never resend.
    elif boundary == "after_result_stored":
        assert after[1] == 1  # Replay Observation, not the search.
    elif boundary == "command_dispatched":
        assert after == (2, 1, 2)
    elif boundary == "graph_finished":
        assert after == (2, 1, 2)  # Terminal Graph has zero added investigation calls.
    assert (
        run(directory, "replay")["result"]["duplicate"] and counts(directory) == after
    )


def test_process_owned_conversation_lock_blocks_only_until_owner_exits(directory):
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.entry_recovery_cli", str(directory), "hold"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not (directory / "lock-ready").exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        busy = run(directory, "hello")
        assert "ENTRY_CONVERSATION_BUSY" in busy["result"]["limitations"] and counts(
            directory
        ) == (0, 0, 0)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert run(directory, "hello")["result"]["status"] == "REPLY" and counts(
        directory
    ) == (1, 0, 0)
    store = EntryStore(directory / "tasks.db")
    budget = EntryBudgetStore(store)
    policy, _ = budget.frozen(CONVERSATION)
    assert budget.snapshot(CONVERSATION, policy)["external_spent"] == 1


def test_final_reply_loss_replays_current_command_without_duplicate_call(directory):
    run(directory, "start", "receipt_saved", 73)
    run(directory, "recover")
    before = counts(directory)
    replay = run(directory, "replay")
    assert replay["result"]["duplicate"] and counts(directory) == before
    assert before[0] == 1 and before[1] <= 1
    again = run(directory, "replay")
    assert again["result"]["duplicate"] and counts(directory) == before
