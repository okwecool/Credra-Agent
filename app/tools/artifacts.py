"""Run-scoped artifact persistence with stable, traversal-safe references."""

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel


class ArtifactStore:
    """Read and write versioned artifacts beneath one execution directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.artifact_dir = self.run_dir / "artifacts"

    def _resolve_reference(self, reference: str) -> Path:
        normalized = PurePosixPath(reference)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError(f"invalid artifact reference: {reference}")
        if len(normalized.parts) != 2 or normalized.parts[0] != "artifacts":
            raise ValueError("artifact references must be under artifacts/")

        target = (self.run_dir / Path(*normalized.parts)).resolve()
        if target.parent != self.artifact_dir:
            raise ValueError(f"artifact path escapes run directory: {reference}")
        return target

    def write_json(self, reference: str, value: BaseModel | dict[str, Any]) -> str:
        target = self._resolve_reference(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if target.exists():
            if target.read_text(encoding="utf-8") != serialized:
                raise FileExistsError(
                    f"artifact already exists with other content: {reference}"
                )
            return reference
        target.write_text(serialized, encoding="utf-8")
        return reference

    @staticmethod
    def next_version_reference(
        base_name: str,
        current_reference: str | None,
        suffix: str = ".json",
    ) -> str:
        """Derive the next stable version from the reference stored in State."""

        if current_reference is None:
            version = 1
        else:
            match = re.fullmatch(
                rf"artifacts/{re.escape(base_name)}_v(\d+){re.escape(suffix)}",
                current_reference,
            )
            if match is None:
                raise ValueError(f"unexpected artifact reference: {current_reference}")
            version = int(match.group(1)) + 1
        return f"artifacts/{base_name}_v{version}{suffix}"

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
