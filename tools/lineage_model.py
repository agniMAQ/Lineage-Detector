"""Canonical row contract for Lineage Viewer CSV outputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


LINEAGE_COLUMNS = (
    "Source_Schema",
    "Source_Object",
    "Source_Type",
    "Target_Schema",
    "Target_Object",
    "Target_Type",
    "Dependency_Type",
    "Column_Or_Parameter",
    "Schema_Bound",
    "External_Location",
    "Is_Ambiguous",
    "Location_Type",
    "Source_Platform",
    "Target_Platform",
    "Transform_Tool",
    "Transform_Name",
    "Transform_Path",
    "Source_Column",
)

UPPERCASE_COLUMNS = {
    "Dependency_Type",
    "Location_Type",
    "Source_Platform",
    "Target_Platform",
}

NULLABLE_COLUMNS = {
    "Source_Schema",
    "Target_Schema",
    "External_Location",
    "Transform_Name",
    "Transform_Path",
    "Source_Column",
}


def _normalize_flag(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in {"yes", "true", "1"}:
        return "Yes"
    if text in {"no", "false", "0"}:
        return "No"
    return str(value or "").strip()


def normalize_row(row: Mapping[str, Any] | Sequence[Any]) -> dict[str, str]:
    """Normalize values while retaining shape errors for validation to detect."""
    if isinstance(row, Mapping):
        normalized = {str(key): value for key, value in row.items()}
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        if len(row) != len(LINEAGE_COLUMNS):
            return {f"Column_{index}": value for index, value in enumerate(row)}
        normalized = dict(zip(LINEAGE_COLUMNS, row))
    else:
        return {"Invalid_Row": row}

    for column in LINEAGE_COLUMNS:
        if column not in normalized:
            continue
        value = normalized[column]
        text = str(value if value is not None else "").strip()
        if not text and column in NULLABLE_COLUMNS:
            text = "NULL"
        if column in {"Schema_Bound", "Is_Ambiguous"}:
            text = _normalize_flag(text)
        elif column in UPPERCASE_COLUMNS:
            text = text.upper()
        normalized[column] = text
    return normalized


def normalize_rows(rows: Sequence[Mapping[str, Any] | Sequence[Any]]) -> list[dict[str, str]]:
    return [normalize_row(row) for row in rows]
