"""P25 isolated browser acceptance app: fixed model + real MCP mock provider.

Run from repository root: chainlit run tests/ui_browser_app.py --port 8013.
This harness never builds a live model or a paid search provider.
"""

import json
import shutil
from pathlib import Path

import app.chainlit_app as ui
import credra_agent.runtime.ui_service as service
from app.config import Settings
from credra_agent.observability.runtime import (
    config_from_settings,
    start_process_service,
)
from tests.test_v2_ui_execution import policy_dict
from tests.ui_execution_cli import OfflineModel

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "checkpoints" / ".p25-browser"
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
