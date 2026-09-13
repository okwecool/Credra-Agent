"""Schema diagnostics without model input, Pydantic messages or arbitrary keys."""

import re

from .events import reference_id
from .text import ISSUE_LABELS


def schema_issues(error, output_schema):
    schema = output_schema.model_json_schema()
    field_names = set()

    def visit(value):
        if isinstance(value, dict):
            field_names.update(value.get("properties", {}).keys())
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    errors = error.errors(include_url=False, include_context=True, include_input=False)
    issues = []
    for item in errors[:20]:
        path = [
            part
            if type(part) is int
            and 0 <= part <= 10**15
            or isinstance(part, str)
            and part in field_names
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", part)
            else reference_id(part)
            for part in item["loc"][:12]
        ]
        issue = {
            "path": path,
            "kind": item["type"] if item["type"] in ISSUE_LABELS else "other",
        }
        context = item.get("ctx", {})
        for key in ("max_length", "min_length"):
            value = context.get(key)
            if type(value) is int and 0 <= value <= 10**15:
                issue["limit"] = value
        if (
            type(context.get("actual_length")) is int
            and 0 <= context["actual_length"] <= 10**15
        ):
            issue["actual_length"] = context["actual_length"]
        issues.append(issue)
    return {"validation_issues": issues, "validation_issue_count": len(errors)}
