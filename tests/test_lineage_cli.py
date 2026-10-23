from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from lineage_cli import run_generate  # noqa: E402
from lineage_model import LINEAGE_COLUMNS  # noqa: E402
from parser_registry import PARSER_REGISTRY  # noqa: E402


class LineageCliTests(unittest.TestCase):
    @staticmethod
    def _llm_row(source_object="Customers", target_object="Customers"):
        return {
            "Source_Schema": "dbo",
            "Source_Object": source_object,
            "Source_Type": "TABLE",
            "Target_Schema": "sales",
            "Target_Object": target_object,
            "Target_Type": "TABLE",
            "Dependency_Type": "DATA_FLOW",
            "Column_Or_Parameter": "*",
            "Schema_Bound": "No",
            "External_Location": "NULL",
            "Is_Ambiguous": "No",
            "Location_Type": "REMOTE",
            "Source_Platform": "AZURE",
            "Target_Platform": "FABRIC_LAKEHOUSE",
            "Transform_Tool": "MODEL_VALUE",
            "Transform_Name": "Copy Customers",
            "Transform_Path": "MODEL_VALUE",
            "Source_Column": "NULL",
        }

    def test_fabric_repository_runs_through_shared_cli(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notebook = (
                root
                / "Fabric"
                / "Silver"
                / "Notebooks"
                / "LoadOrders.Notebook"
                / "notebook-content.py"
            )
            notebook.parent.mkdir(parents=True)
            notebook.write_text(
                'source = spark.table("Bronze_LH.raw.orders")\n'
                'source.write.saveAsTable("Silver_LH.sales.orders")\n',
                encoding="utf-8",
            )

            result = run_generate(root)

            self.assertEqual("SUCCESS", result.status)
            self.assertEqual(1, result.consolidated_rows)
            with result.consolidated_file.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("FABRIC_BRONZE", rows[0]["Source_Platform"])
            self.assertEqual("FABRIC_SILVER", rows[0]["Target_Platform"])

    def test_fabric_pipeline_runs_through_shared_cli(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pipeline = root / "Fabric" / "Load Orders.DataPipeline" / "pipeline-content.json"
            pipeline.parent.mkdir(parents=True)
            pipeline.write_text(
                json.dumps({
                    "properties": {
                        "activities": [{
                            "name": "Copy Orders",
                            "type": "Copy",
                            "typeProperties": {
                                "source": {
                                    "type": "AzureSqlSource",
                                    "datasetSettings": {
                                        "type": "AzureSqlTable",
                                        "typeProperties": {"schema": "dbo", "table": "Orders"},
                                        "connectionSettings": {
                                            "name": "SalesDb",
                                            "properties": {"type": "AzureSqlDatabase"},
                                        },
                                    },
                                },
                                "sink": {
                                    "type": "LakehouseTableSink",
                                    "datasetSettings": {
                                        "type": "LakehouseTable",
                                        "typeProperties": {"schema": "sales", "table": "Orders"},
                                        "connectionSettings": {
                                            "name": "Gold_LH",
                                            "properties": {"type": "Lakehouse"},
                                        },
                                    },
                                },
                            },
                        }],
                    },
                }),
                encoding="utf-8",
            )

            result = run_generate(root)

            self.assertEqual("SUCCESS", result.status)
            self.assertEqual(2, result.consolidated_rows)
            with result.consolidated_file.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {"DATA_FLOW", "ORCHESTRATION"},
                {row["Dependency_Type"] for row in rows},
            )
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("Status: **SUCCESS**", report)
            self.assertNotIn("unsupported-pipeline-definition", report)

    def test_unresolved_pipeline_copy_endpoint_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pipeline = root / "Broken Copy.DataPipeline" / "pipeline-content.json"
            pipeline.parent.mkdir()
            pipeline.write_text(
                json.dumps({
                    "properties": {
                        "activities": [{
                            "name": "Copy Unknown",
                            "type": "Copy",
                            "typeProperties": {"source": {}, "sink": {}},
                        }],
                    },
                }),
                encoding="utf-8",
            )

            result = run_generate(root)

            self.assertEqual("PARTIAL", result.status)
            self.assertEqual(1, result.consolidated_rows)
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("Copy activity endpoint pair(s) could not be resolved", report)

    def test_mixed_repository_writes_parser_consolidated_and_report_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "load_customers.py").write_text(
                'import pandas as pd\n'
                'df = pd.read_csv("input/customers.csv")\n'
                'df.to_csv("output/customers.csv")\n',
                encoding="utf-8",
            )
            (root / "customer_view.sql").write_text(
                "create view curated.customer_view as "
                "select * from raw.customers;",
                encoding="utf-8",
            )
            (root / "unknown_pipeline.json").write_text(
                '{"activities": [], "source": {}, "sink": {}}',
                encoding="utf-8",
            )

            result = run_generate(root)

            self.assertEqual("PARTIAL", result.status)
            self.assertGreaterEqual(result.consolidated_rows, 2)
            output = root / "lineage-output"
            self.assertTrue((output / "lineage_metadata_python.csv").is_file())
            self.assertTrue((output / "lineage_metadata_sql.csv").is_file())
            self.assertTrue(result.consolidated_file.is_file())
            self.assertTrue(result.report_file.is_file())
            with result.consolidated_file.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(LINEAGE_COLUMNS), reader.fieldnames)
                rows = list(reader)
            self.assertEqual(result.consolidated_rows, len(rows))
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("Status: **PARTIAL**", report)
            self.assertIn("unknown_pipeline.json", report)

    def test_llm_poc_routes_only_unsupported_pipeline_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "load_customers.py").write_text(
                'import pandas as pd\n'
                'df = pd.read_csv("input/customers.csv")\n'
                'df.to_csv("output/customers.csv")\n',
                encoding="utf-8",
            )
            (root / "customer_view.sql").write_text(
                "create view curated.customer_view as select * from raw.customers;",
                encoding="utf-8",
            )
            fabric_pipeline = (
                root / "Fabric" / "Owned.DataPipeline" / "pipeline-content.json"
            )
            fabric_pipeline.parent.mkdir(parents=True)
            fabric_pipeline.write_text(
                json.dumps({"properties": {"activities": []}}),
                encoding="utf-8",
            )
            (root / "adf_pipeline.json").write_text(
                json.dumps({
                    "name": "LoadCustomers",
                    "activities": [{"name": "Copy Customers", "type": "Copy"}],
                }),
                encoding="utf-8",
            )
            (root / "orders_workflow.yaml").write_text(
                "source: raw.orders\ntarget: curated.orders\n",
                encoding="utf-8",
            )
            request_prompts = []

            def fake_post(endpoint, api_key, payload, timeout_seconds):
                request_prompts.append(payload["messages"][1]["content"])
                row = self._llm_row()
                return {
                    "choices": [{
                        "message": {"content": json.dumps({"rows": [row]})},
                    }],
                }

            environment = {
                "LINEAGE_LLM_ENDPOINT": "https://llm.example.test/chat/completions",
                "LINEAGE_LLM_API_KEY": "test-key",
                "LINEAGE_LLM_MODEL": "test-model",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "llm_pipeline_poc_parser._post_json",
                side_effect=fake_post,
            ):
                result = run_generate(root, llm_poc=True)

            self.assertEqual("SUCCESS", result.status)
            self.assertEqual(2, len(request_prompts))
            prompts = "\n".join(request_prompts)
            self.assertIn("adf_pipeline.json", prompts)
            self.assertIn("orders_workflow.yaml", prompts)
            self.assertNotIn("pipeline-content.json", prompts)
            self.assertNotIn("load_customers.py", prompts)
            self.assertNotIn("customer_view.sql", prompts)

            output = root / "lineage-output"
            llm_output = output / "lineage_metadata_llm_pipeline_poc.csv"
            self.assertTrue(llm_output.is_file())
            with llm_output.open(encoding="utf-8", newline="") as handle:
                llm_rows = list(csv.DictReader(handle))
            self.assertEqual(2, len(llm_rows))
            self.assertTrue(
                all(row["Transform_Tool"] == "LLM_PIPELINE_POC" for row in llm_rows)
            )
            self.assertTrue(all(row["Is_Ambiguous"] == "Yes" for row in llm_rows))

            self.assertIsNotNone(result.poc_consolidated_file)
            self.assertEqual(result.consolidated_rows + 2, result.poc_consolidated_rows)
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("## LLM Pipeline POC", report)
            self.assertIn("- Eligible files: 2", report)
            self.assertIn("- Model requests: 2", report)
            self.assertIn("## LLM Fallback Coverage", report)
            self.assertEqual(
                2, report.count("Not covered by default parsers, used LLM"),
            )
            unsupported = report.split("## Unsupported Candidates", 1)[1].split(
                "## Diagnostics", 1
            )[0]
            self.assertNotIn("adf_pipeline.json", unsupported)
            self.assertNotIn("orders_workflow.yaml", unsupported)
            self.assertIn("None.", unsupported)

    def test_llm_poc_rejects_invalid_row_and_preserves_deterministic_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "load.py").write_text(
                'import pandas as pd\n'
                'df = pd.read_csv("input.csv")\n'
                'df.to_csv("output.csv")\n',
                encoding="utf-8",
            )
            (root / "adf_pipeline.json").write_text(
                '{"activities": [{"type": "Copy"}]}',
                encoding="utf-8",
            )
            invalid_row = self._llm_row(source_object="")
            response = {
                "choices": [{
                    "message": {"content": json.dumps({"rows": [invalid_row]})},
                }],
            }
            environment = {
                "LINEAGE_LLM_ENDPOINT": "https://llm.example.test/chat/completions",
                "LINEAGE_LLM_API_KEY": "test-key",
                "LINEAGE_LLM_MODEL": "test-model",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "llm_pipeline_poc_parser._post_json",
                return_value=response,
            ):
                result = run_generate(root, llm_poc=True)

            self.assertEqual(1, result.consolidated_rows)
            self.assertEqual(1, result.poc_consolidated_rows)
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("- Rejected rows: 1", report)
            self.assertIn("Source_Object is required", report)

    def test_default_run_never_invokes_llm_poc_parser(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "load.py").write_text(
                'import pandas as pd\n'
                'df = pd.read_csv("input.csv")\n'
                'df.to_csv("output.csv")\n',
                encoding="utf-8",
            )

            with patch("llm_pipeline_poc_parser.parse_candidates") as parser:
                result = run_generate(root)

            parser.assert_not_called()
            self.assertEqual("SUCCESS", result.status)
            self.assertIsNone(result.poc_consolidated_file)

    def test_copilot_backend_uses_existing_validation_and_poc_outputs(self):
        for row, error in [
            (self._llm_row(), None),
            (self._llm_row(source_object=""), None),
            (None, RuntimeError("Copilot is not authenticated")),
        ]:
            with self.subTest(row=row, error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "load.py").write_text(
                    'import pandas as pd\n'
                    'df = pd.read_csv("input.csv")\n'
                    'df.to_csv("output.csv")\n',
                    encoding="utf-8",
                )
                (root / "adf_pipeline.json").write_text(
                    '{"activities": [{"type": "Copy"}]}', encoding="utf-8",
                )
                with (
                    patch.dict(os.environ, {
                        "LINEAGE_LLM_PROVIDER": "copilot",
                        "LINEAGE_COPILOT_MODEL": "sonnet-test",
                    }, clear=True),
                    patch(
                        "copilot_backend.complete",
                        return_value=json.dumps({"rows": [row]}),
                        side_effect=error,
                    ) as complete,
                    patch("llm_pipeline_poc_parser._post_json") as http_post,
                ):
                    result = run_generate(root, llm_poc=True)
                complete.assert_called_once()
                http_post.assert_not_called()
                self.assertEqual(1, result.consolidated_rows)
                valid = row is not None and bool(row["Source_Object"])
                self.assertEqual(2 if valid else 1, result.poc_consolidated_rows)
                self.assertEqual("SUCCESS" if valid else "PARTIAL", result.status)
                report = result.report_file.read_text(encoding="utf-8")
                self.assertEqual(
                    valid, "Not covered by default parsers, used LLM" in report,
                )
                if error:
                    self.assertIn("Copilot is not authenticated", report)
                elif not valid:
                    self.assertIn("Source_Object is required", report)
                else:
                    with (root / "lineage-output" / "lineage_metadata_llm_pipeline_poc.csv").open(
                        encoding="utf-8", newline="",
                    ) as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual("LLM_PIPELINE_POC", rows[0]["Transform_Tool"])
                    self.assertEqual("Yes", rows[0]["Is_Ambiguous"])

    def test_llm_coverage_keeps_failed_empty_and_partially_rejected_files_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filenames = [
                "good_pipeline.json", "bad_pipeline.json", "partial_pipeline.json",
                "empty_pipeline.json", "failed_pipeline.json",
            ]
            for filename in filenames:
                (root / filename).write_text(
                    '{"activities": [{"type": "Copy"}]}', encoding="utf-8",
                )

            def fake_post(endpoint, api_key, payload, timeout_seconds):
                prompt = payload["messages"][1]["content"]
                if "File path: failed_pipeline.json" in prompt:
                    raise RuntimeError("simulated model error")
                if "File path: empty_pipeline.json" in prompt:
                    rows = []
                elif "File path: bad_pipeline.json" in prompt:
                    rows = [self._llm_row(source_object="")]
                elif "File path: partial_pipeline.json" in prompt:
                    rows = [self._llm_row(), self._llm_row(source_object="")]
                else:
                    rows = [self._llm_row()]
                return {"choices": [{"message": {"content": json.dumps({"rows": rows})}}]}

            with (
                patch.dict(os.environ, {
                    "LINEAGE_LLM_ENDPOINT": "https://llm.example.test/chat/completions",
                    "LINEAGE_LLM_API_KEY": "test-key",
                    "LINEAGE_LLM_MODEL": "test-model",
                }, clear=True),
                patch("llm_pipeline_poc_parser._post_json", side_effect=fake_post),
            ):
                result = run_generate(root, llm_poc=True)

            self.assertEqual("PARTIAL", result.status)
            report = result.report_file.read_text(encoding="utf-8")
            coverage, unsupported_and_diagnostics = report.split(
                "## LLM Fallback Coverage", 1
            )[1].split("## Unsupported Candidates", 1)
            unsupported = unsupported_and_diagnostics.split("## Diagnostics", 1)[0]
            self.assertIn("good_pipeline.json", coverage)
            self.assertNotIn("good_pipeline.json", unsupported)
            for filename in filenames[1:]:
                self.assertNotIn(filename, coverage)
                self.assertIn(filename, unsupported)
            self.assertIn("simulated model error", report)
            self.assertIn("Source_Object is required", report)
            self.assertIn("- Unresolved files: 1", report)
            self.assertIn("- Rejected rows: 2", report)

    def test_failed_parser_does_not_discard_independent_results(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "load.py").write_text(
                'import pandas as pd\n'
                'df = pd.read_csv("input.csv")\n'
                'df.to_csv("output.csv")\n',
                encoding="utf-8",
            )
            (root / "broken.sql").write_text(
                "create view curated.v as select * from raw.t;",
                encoding="utf-8",
            )

            def fail_adapter(context, candidates):
                raise RuntimeError("simulated parser failure")

            registry = tuple(
                replace(spec, adapter=fail_adapter) if spec.parser_id == "sql" else spec
                for spec in PARSER_REGISTRY
            )
            result = run_generate(root, registry=registry)

            self.assertEqual("PARTIAL", result.status)
            self.assertGreater(result.consolidated_rows, 0)
            report = result.report_file.read_text(encoding="utf-8")
            self.assertIn("simulated parser failure", report)
            self.assertIn("| sql | FAILED |", report)

    def test_talend_tableau_and_powerbi_adapters_emit_canonical_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "LoadCustomers_0.1.item").write_text(
                """<talendfile>
<node componentName="tFileInputDelimited">
  <elementParameter name="UNIQUE_NAME" value="input_customers" />
  <elementParameter name="FILENAME" value="input/customers.csv" />
</node>
<node componentName="tRedshiftOutput">
  <elementParameter name="UNIQUE_NAME" value="output_customers" />
  <elementParameter name="SCHEMANAME" value="curated" />
  <elementParameter name="TABLENAME" value="customers" />
</node>
</talendfile>""",
                encoding="utf-8",
            )
            with (root / "talend_pipelines_inventory.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "pipeline_name",
                        "display_name",
                        "relative_item_path",
                        "folder",
                        "source_components",
                        "target_components",
                        "components",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "pipeline_name": "LoadOrders",
                    "display_name": "LoadOrders",
                    "relative_item_path": "jobs/LoadOrders.item",
                    "folder": "Orders",
                    "source_components": "tFileInputDelimited",
                    "target_components": "tRedshiftOutput",
                    "components": "tFileInputDelimited;tRedshiftOutput",
                })
            with (root / "tableau_metadata.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "Project",
                        "Workbook",
                        "Table Name",
                        "Source System Type",
                        "Source Table(s) Used",
                        "Source Queries",
                        "Server / Host Address",
                        "Database",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "Project": "Sales",
                    "Workbook": "Customer Overview",
                    "Table Name": "Customers",
                    "Source System Type": "Amazon Redshift",
                    "Source Table(s) Used": "curated.customers",
                    "Server / Host Address": "redshift.example.test",
                    "Database": "analytics",
                })
            powerbi_header = [*LINEAGE_COLUMNS, "Source_Parser"]
            with (root / "lineage_metadata_powerbi.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=powerbi_header)
                writer.writeheader()
                writer.writerow({
                    "Source_Schema": "POWER_BI_MODEL",
                    "Source_Object": "Sales Model",
                    "Source_Type": "SEMANTIC_MODEL",
                    "Target_Schema": "POWER_BI_REPORTS",
                    "Target_Object": "Sales Report",
                    "Target_Type": "POWER_BI_REPORT",
                    "Dependency_Type": "DATA_FLOW",
                    "Column_Or_Parameter": "*",
                    "Schema_Bound": "No",
                    "External_Location": "NULL",
                    "Is_Ambiguous": "No",
                    "Location_Type": "LOCAL",
                    "Source_Platform": "POWER_BI",
                    "Target_Platform": "POWER_BI",
                    "Transform_Tool": "POWER_BI_REPORT",
                    "Transform_Name": "Sales Report",
                    "Transform_Path": "powerbi/Sales Report",
                    "Source_Column": "NULL",
                    "Source_Parser": "POWER_BI",
                })

            result = run_generate(root)

            self.assertEqual("SUCCESS", result.status)
            self.assertEqual(5, result.consolidated_rows)
            output = root / "lineage-output"
            self.assertTrue((output / "lineage_metadata_talend.csv").is_file())
            self.assertTrue((output / "lineage_metadata_tableau.csv").is_file())
            self.assertTrue((output / "lineage_metadata_powerbi.csv").is_file())
            with result.consolidated_file.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(all(len(row) == len(LINEAGE_COLUMNS) for row in rows))
            self.assertIn("POWER_BI_MODEL", {row["Source_Platform"] for row in rows})


if __name__ == "__main__":
    unittest.main()