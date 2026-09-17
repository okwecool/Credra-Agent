"""M1 structured Case template tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.cases import create_case_template


def test_template_uses_creation_date_for_source_access_metadata(tmp_path: Path) -> None:
    case_dir = create_case_template("case_template_date", tmp_path / "data")
    manifest_path = case_dir / "source" / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "source_manifest_v2"
    assert manifest["scope"]["accessed_at"] == datetime.now(UTC).date().isoformat()
