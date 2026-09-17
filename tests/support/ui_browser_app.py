"""P25 isolated browser acceptance app: fixed model + real MCP mock provider.

Run from repository root: chainlit run tests/support/ui_browser_app.py --port 8013.
This harness never builds a live model or a paid search provider.
"""

import json
import os
import shutil
from pathlib import Path

import app.chainlit_app as ui
import credra_agent.runtime.ui_service as service
from app.config import Settings
from credra_agent.observability.runtime import (
    config_from_settings,
    start_process_service,
)
from tests.entry.test_v2_ui_execution import policy_dict
from tests.support.ui_execution_cli import OfflineModel

ROOT = Path(__file__).resolve().parents[2]
ENTRY_MODE = os.environ.get("CREDRA_BROWSER_PHASE") == "P26"
DIRECTORY = ROOT / "checkpoints" / (".p26-browser" if ENTRY_MODE else ".p25-browser")
DIRECTORY.mkdir(parents=True, exist_ok=True)
for case in ("case_byd_002594", "case_saic_600104"):
    shutil.copytree(
        ROOT / "data" / case / "source",
        DIRECTORY / "data" / case / "source",
        dirs_exist_ok=True,
    )
(DIRECTORY / "policy.json").write_text(json.dumps(policy_dict()), encoding="utf-8")
settings = Settings(
    _env_file=None,
    data_dir=DIRECTORY / "data",
    checkpoint_db_path=DIRECTORY / "tasks.db",
    trace_dir=DIRECTORY / "traces",
    service_log_dir=DIRECTORY / "logs",
    agent_ui_execution_enabled=True,
    agent_ui_policy_path=DIRECTORY / "policy.json",
    intent_mode="llm",
    analysis_mode="llm",
    model_base_url="https://offline.invalid/v1",
    model_name="offline-placeholder",
    model_api_key="test-placeholder",
    research_provider="mock",
    content_fetch_provider="disabled",
    fact_verifier="rules",
    research_fail_first=False,
)
ui.get_settings = lambda: settings
original_description = ui.ui_execution_description
ui.ui_execution_description = lambda s: (
    "离线浏览器验收：固定模型 + MCP Mock；没有真实模型或付费搜索调用。\n\n"
    + original_description(s)
)
service.build_ui_models = lambda s, p: (
    OfflineModel(DIRECTORY),
    OfflineModel(DIRECTORY),
)
start_process_service("p25_offline_browser", config_from_settings(settings))

if ENTRY_MODE:
    from datetime import date

    import credra_agent.entry.service as entry_service
    from app.llm.gateway import StructuredModelResult
    from credra_agent.entry import delegation
    from credra_agent.entry.models import EntryDecision
    from credra_agent.intent.catalog import load_subject_catalog
    from credra_agent.intent.parser import parse_draft
    from tests.entry.test_v2_entry_delegation import CONTROLS
    from tests.entry.test_v2_entry_dialogue import policy_data

    path = DIRECTORY / "entry.json"
    path.write_text(
        json.dumps(policy_data(allowed_control_tools=CONTROLS)), encoding="utf-8"
    )
    settings.agent_entry_policy_path = path

    class BrowserEntryModel:
        model_name = "p26-offline-browser"

        def generate(self, **kwargs):
            context = kwargs["payload"]
            query = context["query"]
            if context["turn_events"]:
                output = {
                    "decision": "reply",
                    "content_kind": "task_facts",
                    "text": "查询快照",
                    "fact_refs": context["turn_events"][-1]["payload"]["fact_refs"],
                }
            elif "你好" in query:
                output = {
                    "decision": "reply",
                    "content_kind": "conversation",
                    "text": "你好，有什么可以帮你？",
                }
            elif "继续" in query and context["current_task"]:
                selected = context["current_task"]["summary"]
                output = {
                    "decision": "call_tool",
                    "tool": "resume_investigation",
                    "arguments": {
                        "task_id": selected["task_id"],
                        "expected_state_ref": selected["state_ref"],
                    },
                    "reason": "明确恢复原任务",
                }
            elif "任务" in query or "状态" in query:
                selected = context["selected_task_id"]
                output = {
                    "decision": "call_tool",
                    "tool": "get_task_status"
                    if selected and "状态" in query
                    else "list_tasks",
                    "arguments": {"task_id": selected}
                    if selected and "状态" in query
                    else {"limit": 20},
                    "reason": "读取实际任务",
                }
            else:
                draft = parse_draft(
                    query,
                    catalog=load_subject_catalog(settings.data_dir),
                    anchor_date=date.fromisoformat(context["anchor_date"]),
                )[0]
                output = {
                    "decision": "call_tool",
                    "tool": "prepare_investigation",
                    "arguments": {"draft": draft.model_dump(mode="json")},
                    "reason": "离线调查委派",
                }
            return StructuredModelResult(
                output=EntryDecision.model_validate(output),
                model_name=self.model_name,
                attempts=1,
                external_requests=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=20,
                accounting_complete=True,
            )

    entry_service.build_entry_model = lambda s, p: BrowserEntryModel()
    delegation.build_investigation_model = lambda s, p: OfflineModel(DIRECTORY)
    ui.ui_execution_description = lambda s: (
        "P26 离线浏览器验收：上下文模型替身 + 真实 Runtime/MCP Mock；不发生真实付费调用。"
    )
