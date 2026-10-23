"""Validation for normalized lineage rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lineage_model import LINEAGE_COLUMNS


DEPENDENCY_TYPES = {"DATA_FLOW", "ORCHESTRATION", "SHORTCUT"}
LOCATION_TYPES = {"LOCAL", "EXTERNAL", "REMOTE"}
BOOLEAN_VALUES = {"Yes", "No"}


@dataclass(frozen=True)
class RejectedRow:
    index: int
    reasons: tuple[str, ...]
    row: dict[str, Any]


@dataclass(frozen=True)
class ValidationResult:
    valid_rows: list[dict[str, str]]
    rejected_rows: list[RejectedRow]
    warnings: list[str]


def validate_rows(rows: list[dict[str, Any]]) -> ValidationResult:
    valid_rows: list[dict[str, str]] = []
    rejected_rows: list[RejectedRow] = []
    warnings: list[str] = []
    required_columns = set(LINEAGE_COLUMNS)
    missing_evidence_paths = 0

    for index, row in enumerate(rows, start=1):
        reasons: list[str] = []
        actual_columns = set(row)
        missing = sorted(required_columns - actual_columns)
        extra = sorted(actual_columns - required_columns)
        if missing:
            reasons.append(f"missing columns: {', '.join(missing)}")
        if extra:
            reasons.append(f"unexpected columns: {', '.join(extra)}")

        for column in ("Source_Object", "Target_Object"):
            value = str(row.get(column, "")).strip()
            if not value or value.upper() == "NULL":
                reasons.append(f"{column} is required")

        dependency_type = str(row.get("Dependency_Type", "")).strip()
        if dependency_type not in DEPENDENCY_TYPES:
            reasons.append(f"unsupported Dependency_Type: {dependency_type or '<empty>'}")

        location_type = str(row.get("Location_Type", "")).strip()
        if location_type not in LOCATION_TYPES:
            reasons.append(f"unsupported Location_Type: {location_type or '<empty>'}")

        for column in ("Schema_Bound", "Is_Ambiguous"):
            value = str(row.get(column, "")).strip()
            if value not in BOOLEAN_VALUES:
                reasons.append(f"{column} must be Yes or No")

        for column in ("Source_Type", "Target_Type", "Source_Platform", "Target_Platform"):
            value = str(row.get(column, "")).strip()
            if not value or value.upper() == "NULL":
                reasons.append(f"{column} is required")

        transform_path = str(row.get("Transform_Path", "")).strip()
        if transform_path.upper() in {"", "NULL"}:
            missing_evidence_paths += 1

        if reasons:
            rejected_rows.append(RejectedRow(index, tuple(reasons), dict(row)))
        else:
            valid_rows.append({column: str(row[column]) for column in LINEAGE_COLUMNS})

    if missing_evidence_paths:
        warnings.append(
            f"transform evidence path is unavailable for {missing_evidence_paths} row(s)"
        )
    return ValidationResult(valid_rows, rejected_rows, warnings)
