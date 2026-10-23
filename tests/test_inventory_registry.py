from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from inventory import build_inventory  # noqa: E402
from lineage_model import LINEAGE_COLUMNS  # noqa: E402
from parser_registry import select_parsers  # noqa: E402


class InventoryRegistryTests(unittest.TestCase):
    def test_mixed_repository_uses_most_specific_ownership(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fabric_notebook = (
                root / "Fabric" / "Silver" / "Notebooks" / "Load.Notebook" / "notebook-content.py"
            )
            fabric_notebook.parent.mkdir(parents=True)
            fabric_notebook.write_text("print('fabric')", encoding="utf-8")
            (root / "transform.py").write_text("print('python')", encoding="utf-8")
            (root / "model.sql").write_text(
                "create view curated.v_customer as select * from raw.customer;",
                encoding="utf-8",
            )
            (root / "job.item").write_text(
                '<talendfile><node componentName="tRedshiftInput" /></talendfile>',
                encoding="utf-8",
            )
            with (root / "tableau.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "Project",
                    "Workbook",
                    "Table Name",
                    "Source System Type",
                    "Source Table(s) Used",
                ])
            (root / "customer_pipeline.json").write_text(
                '{"activities": [], "source": {}, "sink": {}}',
                encoding="utf-8",
            )

            selection = select_parsers(build_inventory(root))

            self.assertEqual(
                {"fabric", "python", "sql", "talend", "tableau"},
                set(selection.assignments),
            )
            self.assertEqual(
                ["transform.py"],
                [candidate.relative_path for candidate in selection.assignments["python"]],
            )
            self.assertEqual(
                ["Fabric/Silver/Notebooks/Load.Notebook/notebook-content.py"],
                [candidate.relative_path for candidate in selection.assignments["fabric"]],
            )
            self.assertEqual(1, len(selection.unsupported))
            self.assertEqual("customer_pipeline.json", selection.unsupported[0].relative_path)

    def test_non_powerbi_lineage_csv_is_not_misrouted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "legacy_tableau_lineage.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[*LINEAGE_COLUMNS, "Source_Parser"])
                writer.writeheader()
                writer.writerow({"Source_Parser": "TABLEAU"})

            selection = select_parsers(build_inventory(root))

            self.assertNotIn("powerbi", selection.assignments)
            self.assertEqual(1, len(selection.unsupported))
            self.assertEqual("unsupported-lineage-csv", selection.unsupported[0].signature)

    def test_fabric_datapipeline_is_owned_by_fabric_parser(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pipeline = root / "Pipelines" / "LoadCustomers.DataPipeline" / "pipeline-content.json"
            pipeline.parent.mkdir(parents=True)
            pipeline.write_text('{"properties": {"activities": []}}', encoding="utf-8")
            generic = root / "generic_pipeline.json"
            generic.write_text('{"activities": []}', encoding="utf-8")

            selection = select_parsers(build_inventory(root))

            self.assertEqual(
                ["Pipelines/LoadCustomers.DataPipeline/pipeline-content.json"],
                [candidate.relative_path for candidate in selection.assignments["fabric"]],
            )
            self.assertEqual(
                ["generic_pipeline.json"],
                [candidate.relative_path for candidate in selection.unsupported],
            )


if __name__ == "__main__":
    unittest.main()