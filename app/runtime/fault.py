"""Deterministic task/tool-scoped fault injection."""

import hashlib
from pathlib import Path


class ResearchFaultInjector:
    """Fail the first call for each task/tool pair when enabled."""

    def __init__(self, state_dir: Path, fail_first: bool) -> None:
        self.state_dir = state_dir
        self.fail_first = fail_first

    def _marker(self, task_id: str, tool_name: str) -> Path:
        digest = hashlib.sha256(f"{task_id}:{tool_name}".encode()).hexdigest()
        return self.state_dir / f"{digest}.attempted"

    def should_fail(self, task_id: str, tool_name: str) -> bool:
        if not self.fail_first:
            return False
        marker = self._marker(task_id, tool_name)
        if marker.exists():
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="ascii")
        return True
