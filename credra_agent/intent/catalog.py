"""Resolve natural-language subjects only against imported Cases."""

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.cases import HIDDEN_DEMO_CASE_IDS


class SubjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    subject_id: str
    subject_name: str
    aliases: list[str]
    available_years: list[int]


_COMMON_ALIASES = {
    "002594": ["比亚迪", "BYD"],
    "600104": ["上汽", "上汽集团", "SAIC"],
}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_subject_catalog(data_dir: Path) -> list[SubjectRecord]:
    records: list[SubjectRecord] = []
    if not data_dir.is_dir():
        return records
    for case_dir in sorted(data_dir.iterdir(), key=lambda item: item.name):
        if not case_dir.is_dir() or case_dir.name in HIDDEN_DEMO_CASE_IDS:
            continue
        source_dir = case_dir / "source"
        manifest = _read_json(source_dir / "source_manifest.json")
        profile = _read_json(source_dir / "company_profile.json")
        financial = _read_json(source_dir / "financial_statement.json")
        company = (
            manifest.get("company") if isinstance(manifest.get("company"), dict) else {}
        )
        name = str(
            company.get("legal_name") or profile.get("company_name") or ""
        ).strip()
        subject_id = str(company.get("a_share_code") or case_dir.name).strip()
        if not name or not subject_id:
            continue
        aliases = [
            name,
            subject_id,
            case_dir.name,
            *_COMMON_ALIASES.get(subject_id, []),
        ]
        for suffix in ("集团股份有限公司", "股份有限公司", "集团有限公司", "有限公司"):
            if name.endswith(suffix) and len(name.removesuffix(suffix)) >= 2:
                aliases.append(name.removesuffix(suffix))
                break
        years = sorted(
            {
                int(item["year"])
                for item in financial.get("statements", [])
                if isinstance(item, dict) and isinstance(item.get("year"), int)
            }
        )
        records.append(
            SubjectRecord(
                case_id=case_dir.name,
                subject_id=subject_id,
                subject_name=name,
                aliases=list(dict.fromkeys(aliases)),
                available_years=years,
            )
        )
    return records


def match_subjects(text: str, catalog: list[SubjectRecord]) -> list[SubjectRecord]:
    matches = [
        item
        for item in catalog
        if any(
            re.search(re.escape(alias), text, flags=re.IGNORECASE)
            for alias in item.aliases
        )
    ]
    unique = {item.subject_id: item for item in matches}
    return list(unique.values())


def resolve_subject(text: str, catalog: list[SubjectRecord]) -> SubjectRecord | None:
    matches = match_subjects(text, catalog)
    return matches[0] if len(matches) == 1 else None
