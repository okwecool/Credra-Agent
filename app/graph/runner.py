"""Execute a workflow while collecting path information outside Graph State."""

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.graph.state import AgentState, initial_state
from app.graph.workflow import build_workflow


@dataclass(frozen=True)
class WorkflowResult:
    state: AgentState
    execution_path: list[str]


def run_workflow(
    case_dir: Path,
    *,
    task_id: str,
    settings: Settings,
) -> WorkflowResult:
    graph = build_workflow(case_dir, settings)
    state = initial_state(task_id, case_dir.name)
    path: list[str] = []
    final_state: AgentState | None = None

    for mode, chunk in graph.stream(state, stream_mode=["updates", "values"]):
        if mode == "updates":
            path.extend(node for node in chunk if not node.startswith("__"))
        elif mode == "values":
            final_state = chunk

    if final_state is None:
        raise RuntimeError("workflow completed without emitting final state")
    return WorkflowResult(state=final_state, execution_path=path)
