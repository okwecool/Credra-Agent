"""Case-scoped artifact persistence with stable, traversal-safe references."""

import json
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel


class ArtifactStore:
    """Read and write versioned artifacts beneath one case directory."""

    def __init__(self, case_dir: Path) -> None:
        self.case_dir = case_dir.resolve()
        self.artifact_dir = self.case_dir / "artifacts"

    def _resolve_reference(self, reference: str) -> Path:
        normalized = PurePosixPath(reference)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError(f"invalid artifact reference: {reference}")
        if len(normalized.parts) != 2 or normalized.parts[0] != "artifacts":
            raise ValueError("artifact references must be under artifacts/")

        target = (self.case_dir / Path(*normalized.parts)).resolve()
        if target.parent != self.artifact_dir:
            raise ValueError(f"artifact path escapes case directory: {reference}")
        return target

    def write_json(self, reference: str, value: BaseModel | dict[str, Any]) -> str:
        target = self._resolve_reference(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return reference

    def read_json(self, reference: str) -> dict[str, Any]:
        target = self._resolve_reference(reference)
        with target.open(encoding="utf-8") as file:
            return json.load(file)

    def write_markdown(self, reference: str, content: str) -> str:
        target = self._resolve_reference(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return reference

    def read_markdown(self, reference: str) -> str:
        return self._resolve_reference(reference).read_text(encoding="utf-8")
