"""Evidence-aware deduplication for lineage rows."""

from __future__ import annotations


DEDUPLICATION_COLUMNS = (
    "Source_Schema",
    "Source_Object",
    "Source_Type",
    "Source_Platform",
    "Target_Schema",
    "Target_Object",
    "Target_Type",
    "Target_Platform",
    "Dependency_Type",
    "Transform_Tool",
    "Transform_Name",
    "Transform_Path",
    "Column_Or_Parameter",
    "Source_Column",
)


def row_identity(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in DEDUPLICATION_COLUMNS)


def deduplicate_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    seen: set[tuple[str, ...]] = set()
    unique_rows: list[dict[str, str]] = []
    for row in rows:
        identity = row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        unique_rows.append(row)
    return unique_rows, len(rows) - len(unique_rows)
