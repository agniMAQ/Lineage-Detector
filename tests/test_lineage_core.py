from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from deduplication import deduplicate_rows  # noqa: E402
from lineage_model import LINEAGE_COLUMNS, normalize_rows  # noqa: E402
from validation import validate_rows  # noqa: E402


def valid_row(**overrides):
    row = {
        "Source_Schema": "raw",
        "Source_Object": "customers",
        "Source_Type": "TABLE",
        "Target_Schema": "curated",
        "Target_Object": "customers",
        "Target_Type": "TABLE",
        "Dependency_Type": "data_flow",
        "Column_Or_Parameter": "*",
        "Schema_Bound": False,
        "External_Location": None,
        "Is_Ambiguous": False,
        "Location_Type": "local",
        "Source_Platform": "redshift",
        "Target_Platform": "fabric",
        "Transform_Tool": "SQL",
        "Transform_Name": "load_customers",
        "Transform_Path": "sql/load_customers.sql",
        "Source_Column": None,
    }
    row.update(overrides)
    return row


class LineageCoreTests(unittest.TestCase):
    def test_normalization_and_validation_preserve_18_column_contract(self):
        normalized = normalize_rows([valid_row()])
        result = validate_rows(normalized)

        self.assertEqual([], result.rejected_rows)
        self.assertEqual(list(LINEAGE_COLUMNS), list(result.valid_rows[0]))
        self.assertEqual("DATA_FLOW", result.valid_rows[0]["Dependency_Type"])
        self.assertEqual("No", result.valid_rows[0]["Is_Ambiguous"])
        self.assertEqual("NULL", result.valid_rows[0]["External_Location"])

    def test_validation_rejects_shape_and_missing_endpoint(self):
        malformed = valid_row(Source_Object="NULL", Source_Platform="NULL")
        malformed["Source_Parser"] = "TEST"

        result = validate_rows(normalize_rows([malformed]))

        self.assertEqual(1, len(result.rejected_rows))
        reasons = " ".join(result.rejected_rows[0].reasons)
        self.assertIn("unexpected columns: Source_Parser", reasons)
        self.assertIn("Source_Object is required", reasons)
        self.assertIn("Source_Platform is required", reasons)

    def test_validation_accepts_pipeline_orchestration(self):
        normalized = normalize_rows([
            valid_row(
                Dependency_Type="orchestration",
                Source_Type="PIPELINE",
                Target_Type="NOTEBOOK",
                Source_Platform="fabric_pipeline",
                Target_Platform="fabric_notebook",
            ),
        ])

        result = validate_rows(normalized)

        self.assertEqual([], result.rejected_rows)
        self.assertEqual("ORCHESTRATION", result.valid_rows[0]["Dependency_Type"])

    def test_missing_evidence_warnings_are_aggregated(self):
        rows = normalize_rows([
            valid_row(Transform_Path="NULL"),
            valid_row(Transform_Path="NULL", Transform_Name="second"),
        ])

        result = validate_rows(rows)

        self.assertEqual(
            ["transform evidence path is unavailable for 2 row(s)"],
            result.warnings,
        )

    def test_deduplication_keeps_distinct_transform_evidence(self):
        first, duplicate, second_transform = normalize_rows([
            valid_row(),
            valid_row(),
            valid_row(Transform_Name="load_customers_v2"),
        ])

        rows, removed = deduplicate_rows([first, duplicate, second_transform])

        self.assertEqual(2, len(rows))
        self.assertEqual(1, removed)


if __name__ == "__main__":
    unittest.main()