"""Manual subprocess smoke test for the default Research MCP stdio transport."""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from app.config import Settings
from app.graph.runner import run_workflow
from app.mcp.research_client import ResearchMCPClient


async def verify_tools() -> None:
    client = ResearchMCPClient(environment={"RESEARCH_PROVIDER": "mock"})
    company = await client.search_company("迅驰供应链科技有限公司")
    industry = await client.search_industry("供应链服务")
    assert company.found and industry.found
    print("stdio_tools=SUCCESS")


def verify_workflow() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary_dir:
        case_dir = Path(temporary_dir) / "case_risky"
        shutil.copytree(
            project_root / "data" / "case_risky" / "source",
            case_dir / "source",
        )
        result = run_workflow(
            case_dir,
            task_id="stdio-risky-001",
            settings=Settings(_env_file=None),
        )
        research_path = case_dir / str(result.state["research_artifact"])
        research = json.loads(research_path.read_text(encoding="utf-8"))
        assert research["status"] == "COMPLETE"
        print("stdio_workflow_path=" + " -> ".join(result.execution_path))
        print("stdio_research_status=" + research["status"])


if __name__ == "__main__":
    os.environ["RESEARCH_PROVIDER"] = "mock"
    asyncio.run(verify_tools())
    verify_workflow()
