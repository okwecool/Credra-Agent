"""Lossless unit normalization and explicit, conservative legacy adaptation."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from credra_agent.financial.models import (
    BALANCE_FIELDS,
    FinancialDatum,
    FinancialInput,
    FinancialSourceRef,
)
from credra_agent.intent.models import Period

LEGACY_FIELDS = (
    "revenue",
    "net_profit",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
    "operating_cash_flow",
)


def normalize_amount(value: Decimal, source_unit: str) -> Decimal:
    """Shift a power of ten without rounding under the caller's Decimal context."""
    shifts = {"CNY": -3, "CNY_1000": 0, "CNY_10K": 1}
    if source_unit not in shifts:
        raise ValueError("FINANCIAL_UNIT_UNSUPPORTED")
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("FINANCIAL_AMOUNT_INVALID")
    sign, digits, exponent = value.as_tuple()
    return Decimal((sign, digits, exponent + shifts[source_unit]))


def adapt_legacy_financial(
    payload: dict,
    *,
    subject_id: str,
    source_hash: str,
    manifest: dict | None = None,
) -> FinancialInput:
    """Legacy net_profit stays attributable to parent owners, never group total.

    Known case imports declare this mapping; an unclassified input remains UNKNOWN.
    Lineage metadata is retained, not upgraded to independent financial verification.
    """
    unit = payload.get("currency")
    if unit not in {"CNY", "CNY_1000", "CNY_10K"}:
        raise ValueError("FINANCIAL_UNIT_UNSUPPORTED")
    rows = payload.get("statements")
    if not isinstance(rows, list) or not rows:
        raise ValueError("FINANCIAL_STATEMENTS_MISSING")
    manifest = manifest or {}
    sources = {item["source_id"]: item for item in manifest.get("sources", [])}
    lineage = manifest.get("field_lineage", [])
    datums = []
    years = []
    for index, row in enumerate(rows):
        year = row["year"]
        if type(year) is not int or not 1900 <= year <= 2200:
            raise ValueError("FINANCIAL_YEAR_INVALID")
        years.append(year)
        period = Period(start=date(year, 1, 1), end=date(year, 12, 31))
        for field in (*LEGACY_FIELDS, "receivables", "net_profit_group_total"):
            is_group = field == "net_profit_group_total"
            metric = "net_profit" if is_group else field
            field_path = f"financial_statement.statements[{year}].{field}"
            matched = [item for item in lineage if field_path in item.get("fields", [])]
            if len(matched) > 1:
                raise ValueError("FINANCIAL_LINEAGE_AMBIGUOUS")
            item = matched[0] if matched else {}
            reported_unit = item.get("source_unit", unit)
            if reported_unit not in {"CNY", "CNY_1000", "CNY_10K"} or (
                reported_unit != unit and item.get("normalized_unit") != unit
            ):
                raise ValueError("FINANCIAL_LINEAGE_UNIT_MISMATCH")
            declaration = item.get("accounting_basis", "")
            basis = (
                "CONSOLIDATED"
                if "合并" in declaration
                else "PARENT"
                if "母公司" in declaration
                else "UNKNOWN"
            )
            attribution = (
                "GROUP_TOTAL"
                if is_group
                else "OWNERS_OF_PARENT"
                if metric == "net_profit"
                and ("归属" in declaration or "归母" in declaration)
                else "UNKNOWN"
                if metric == "net_profit"
                else "NOT_APPLICABLE"
            )
            # The old schema cannot supply receivables or group profit. Do not
            # accept undeclared extension keys as audited financial amounts.
            raw_value = row.get(field) if field in LEGACY_FIELDS else None
            if isinstance(raw_value, (float, bool)):
                raise TypeError("FINANCIAL_AMOUNT_REQUIRES_DECIMAL")
            value = (
                None
                if raw_value is None
                else normalize_amount(Decimal(str(raw_value)), unit)
            )
            source_id = item.get("source_id", "legacy-financial-statement")
            source = sources.get(source_id, {})
            refs = (
                []
                if value is None
                else [
                    FinancialSourceRef(
                        source_id=source_id,
                        location=item.get(
                            "location",
                            f"financial_statement.json#/statements/{index}/{field}",
                        ),
                        published_at=source.get("publication_date"),
                        source_tags=source.get("source_tags", []),
                        input_hash=source_hash,
                        input_location=f"financial_statement.json#/statements/{index}/{field}",
                        reported_unit=reported_unit,
                    )
                ]
            )
            datums.append(
                FinancialDatum(
                    metric=metric,
                    period=period,
                    measurement="BALANCE" if metric in BALANCE_FIELDS else "FLOW",
                    accounting_basis=basis,
                    profit_attribution=attribution,
                    source_unit=unit,
                    revision="restated" if "重述" in declaration else "original",
                    value=value,
                    source_refs=refs,
                    missing_reason=(
                        "MISSING_CONSOLIDATED_PROFIT"
                        if is_group
                        else "LEGACY_FIELD_NOT_DISCLOSED"
                        if field == "receivables"
                        else "LEGACY_AMOUNT_MISSING"
                    )
                    if value is None
                    else None,
                )
            )
    if years != sorted(years) or len(years) != len(set(years)):
        raise ValueError("FINANCIAL_YEARS_NOT_ORDERED")
    return FinancialInput(
        subject_id=subject_id,
        datums=datums,
        limitations=[
            "LEGACY_MISSING_RECEIVABLES_AND_GROUP_PROFIT",
            "SOURCE_METADATA_IS_NOT_VERIFICATION",
        ],
    )


def load_legacy_financial(case_dir: Path, *, subject_id: str) -> FinancialInput:
    """Read only the named case's local files; no model or external requests."""
    source = case_dir.resolve() / "source"
    path = source / "financial_statement.json"
    if path.resolve().parent != source:
        raise ValueError("FINANCIAL_SOURCE_OUTSIDE_CASE")
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"), parse_float=Decimal)
    manifest_path = source / "source_manifest.json"
    if manifest_path.resolve().parent != source:
        raise ValueError("FINANCIAL_MANIFEST_OUTSIDE_CASE")
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else None
    )
    return adapt_legacy_financial(
        payload,
        subject_id=subject_id,
        source_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        manifest=manifest,
    )
