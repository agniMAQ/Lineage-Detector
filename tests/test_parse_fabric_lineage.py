from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PARSER_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "parsers"
    / "parse_fabric_lineage.py"
)


def load_parser():
    spec = importlib.util.spec_from_file_location("parse_fabric_lineage", PARSER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load parser: {PARSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryInputTests(unittest.TestCase):
    def test_arbitrary_repository_generates_existing_csv_contract(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "team-a-fabric-repository"
            notebook = (
                repo
                / "fabric"
                / "Silver"
                / "Notebooks"
                / "sales"
                / "Refresh_sales_orders.Notebook"
                / "notebook-content.py"
            )
            notebook.parent.mkdir(parents=True)
            notebook.write_text(
                'source = spark.table("Bronze_LH.raw.orders")\n'
                'source.write.mode("overwrite").saveAsTable("Silver_LH.sales.orders")\n',
                encoding="utf-8",
            )

            exit_code = parser.main([str(repo)])

            self.assertEqual(exit_code, 0)
            output = repo / "lineage-output"
            consolidated = output / "lineage_metadata_fabric_consolidated.csv"
            self.assertTrue(consolidated.is_file())
            with consolidated.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, parser.CSV_HEADER)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Source_Schema"], "raw")
            self.assertEqual(rows[0]["Source_Object"], "orders")
            self.assertEqual(rows[0]["Source_Platform"], "FABRIC_BRONZE")
            self.assertEqual(rows[0]["Target_Schema"], "sales")
            self.assertEqual(rows[0]["Target_Object"], "orders")
            self.assertEqual(rows[0]["Target_Platform"], "FABRIC_SILVER")

    def test_configuration_supports_nonstandard_layout(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "team-b-repository"
            notebook = (
                repo
                / "fabric-assets"
                / "refined-workloads"
                / "Build_customers.Notebook"
                / "notebook-content.py"
            )
            notebook.parent.mkdir(parents=True)
            notebook.write_text(
                'source = spark.table("bronze_lh.crm.customers")\n'
                'source.write.saveAsTable("silver_lh.crm.customers")\n',
                encoding="utf-8",
            )
            (repo / "lineage.config.json").write_text(
                json.dumps({
                    "artifact_root": "fabric-assets",
                    "layers": {
                        "silver": "fabric-assets/refined-workloads",
                    },
                    "output_dir": "generated-lineage",
                }),
                encoding="utf-8",
            )

            exit_code = parser.main([str(repo)])

            self.assertEqual(exit_code, 0)
            consolidated = (
                repo
                / "generated-lineage"
                / "lineage_metadata_fabric_consolidated.csv"
            )
            with consolidated.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Source_Object"], "customers")
            self.assertEqual(rows[0]["Target_Platform"], "FABRIC_SILVER")

    def test_repository_builds_notebook_model_report_chain(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "end-to-end-fabric-repository"
            notebook = (
                repo
                / "Fabric"
                / "Gold"
                / "Notebooks"
                / "sales"
                / "Build_orders.Notebook"
                / "notebook-content.py"
            )
            notebook.parent.mkdir(parents=True)
            notebook.write_text(
                'source = spark.table("silver_lh.sales.orders")\n'
                'source.write.saveAsTable("gold_lh.sales.orders")\n',
                encoding="utf-8",
            )

            model = (
                repo
                / "Fabric"
                / "Semantic Models"
                / "Sales.SemanticModel"
                / "definition"
                / "tables"
                / "Orders.tmdl"
            )
            model.parent.mkdir(parents=True)
            model.write_text(
                "table Orders\n"
                "    entityName: orders\n"
                "    schemaName: sales\n"
                "    expressionSource: gold_lh\n",
                encoding="utf-8",
            )

            report = (
                repo
                / "Fabric"
                / "Reports"
                / "Sales.Report"
                / "definition.pbir"
            )
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps({
                    "datasetReference": {
                        "byPath": {
                            "path": "../../Semantic Models/Sales.SemanticModel",
                        },
                    },
                }),
                encoding="utf-8",
            )

            exit_code = parser.main([str(repo), "--output-dir", "custom-output"])

            self.assertEqual(exit_code, 0)
            consolidated = (
                repo
                / "custom-output"
                / "lineage_metadata_fabric_consolidated.csv"
            )
            with consolidated.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            edges = {
                (
                    row["Source_Platform"],
                    row["Source_Object"],
                    row["Target_Platform"],
                    row["Target_Object"],
                )
                for row in rows
            }
            self.assertIn(
                ("FABRIC_SILVER", "orders", "FABRIC_GOLD", "orders"),
                edges,
            )
            self.assertIn(
                ("FABRIC_GOLD", "orders", "POWER_BI_TABLE", "Orders"),
                edges,
            )
            self.assertIn(
                ("POWER_BI_TABLE", "Orders", "POWER_BI_MODEL", "Sales"),
                edges,
            )
            self.assertIn(
                ("POWER_BI_MODEL", "Sales", "POWER_BI_REPORT", "Sales"),
                edges,
            )

    def test_repository_parses_fabric_copy_pipeline(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "fabric-pipeline-repository"
            pipeline = (
                repo
                / "Fabric"
                / "Pipelines"
                / "Load Customers.DataPipeline"
                / "pipeline-content.json"
            )
            pipeline.parent.mkdir(parents=True)
            pipeline.write_text(
                json.dumps({
                    "properties": {
                        "activities": [{
                            "name": "Copy Customers",
                            "type": "Copy",
                            "dependsOn": [],
                            "typeProperties": {
                                "source": {
                                    "type": "AzureSqlSource",
                                    "datasetSettings": {
                                        "type": "AzureSqlTable",
                                        "typeProperties": {
                                            "schema": "dbo",
                                            "table": "Customers",
                                        },
                                        "externalReferences": {
                                            "connection": "source-connection-id",
                                        },
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
                                        "typeProperties": {
                                            "schema": "curated",
                                            "table": "Customers",
                                        },
                                        "connectionSettings": {
                                            "name": "Silver_LH",
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

            exit_code = parser.main([str(repo)])

            self.assertEqual(0, exit_code)
            output = repo / "lineage-output"
            pipeline_csv = output / "lineage_metadata_fabric_pipelines.csv"
            with pipeline_csv.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            data_flow = next(row for row in rows if row["Dependency_Type"] == "DATA_FLOW")
            self.assertEqual("dbo", data_flow["Source_Schema"])
            self.assertEqual("Customers", data_flow["Source_Object"])
            self.assertEqual("AZURE", data_flow["Source_Platform"])
            self.assertEqual("Silver_LH.curated", data_flow["Target_Schema"])
            self.assertEqual("Customers", data_flow["Target_Object"])
            self.assertEqual("FABRIC_SILVER", data_flow["Target_Platform"])
            self.assertEqual("FABRIC_PIPELINE", data_flow["Transform_Tool"])
            self.assertEqual("No", data_flow["Is_Ambiguous"])
            self.assertEqual(
                "Fabric/Pipelines/Load Customers.DataPipeline/pipeline-content.json",
                data_flow["Transform_Path"],
            )
            orchestration = next(
                row for row in rows if row["Dependency_Type"] == "ORCHESTRATION"
            )
            self.assertEqual("Load Customers", orchestration["Source_Object"])
            self.assertEqual("Copy Customers", orchestration["Target_Object"])

    def test_pipeline_parses_nested_expressions_and_notebook_references(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "configured-pipeline-repository"
            pipeline_dir = (
                repo
                / "Fabric"
                / "Pipelines"
                / "Orchestrate&#32;Customers.DataPipeline"
            )
            pipeline_dir.mkdir(parents=True)
            (pipeline_dir / ".platform").write_text(
                json.dumps({
                    "metadata": {
                        "type": "DataPipeline",
                        "displayName": "Orchestrate Customers ",
                    },
                    "config": {"logicalId": "pipeline-id"},
                }),
                encoding="utf-8",
            )

            notebook_dir = repo / "Fabric" / "Notebooks" / "Quality Check.Notebook"
            notebook_dir.mkdir(parents=True)
            (notebook_dir / ".platform").write_text(
                json.dumps({
                    "metadata": {"type": "Notebook", "displayName": "Quality Check"},
                    "config": {"logicalId": "quality-notebook-id"},
                }),
                encoding="utf-8",
            )

            lookup = {
                "name": "Find tables",
                "type": "Lookup",
                "typeProperties": {
                    "source": {"type": "ExcelSource"},
                    "datasetSettings": {
                        "type": "Excel",
                        "typeProperties": {
                            "location": {
                                "type": "LakehouseLocation",
                                "folderPath": "control",
                                "fileName": "tables.xlsx",
                            },
                        },
                        "connectionSettings": {
                            "name": "Bronze_LH",
                            "properties": {
                                "type": "Lakehouse",
                                "typeProperties": {"rootFolder": "Files"},
                            },
                        },
                    },
                },
            }
            nested_copy = {
                "name": "Copy table",
                "type": "Copy",
                "typeProperties": {
                    "source": {
                        "type": "AzureSqlSource",
                        "datasetSettings": {
                            "type": "AzureSqlTable",
                            "typeProperties": {
                                "schema": "dbo",
                                "table": {
                                    "value": "@item().TableName",
                                    "type": "Expression",
                                },
                            },
                            "connectionSettings": {
                                "name": "SalesDb",
                                "properties": {"type": "AzureSqlDatabase"},
                            },
                        },
                    },
                    "sink": {
                        "type": "ParquetSink",
                        "datasetSettings": {
                            "type": "Parquet",
                            "typeProperties": {
                                "location": {
                                    "type": "LakehouseLocation",
                                    "folderPath": "customers",
                                    "fileName": {
                                        "value": "@item().TableName",
                                        "type": "Expression",
                                    },
                                },
                            },
                            "connectionSettings": {
                                "name": "Silver_LH",
                                "properties": {
                                    "type": "Lakehouse",
                                    "typeProperties": {"rootFolder": "Files"},
                                },
                            },
                        },
                    },
                },
            }
            loop = {
                "name": "Load each",
                "type": "ForEach",
                "dependsOn": [{
                    "activity": "Find tables",
                    "dependencyConditions": ["Succeeded"],
                }],
                "typeProperties": {"activities": [nested_copy]},
            }
            notebook = {
                "name": "Run quality check",
                "type": "TridentNotebook",
                "dependsOn": [{
                    "activity": "Load each",
                    "dependencyConditions": ["Completed"],
                }],
                "typeProperties": {"notebookId": "quality-notebook-id"},
            }
            (pipeline_dir / "pipeline-content.json").write_text(
                json.dumps({"properties": {"activities": [lookup, loop, notebook]}}),
                encoding="utf-8",
            )
            (repo / "lineage.config.json").write_text(
                json.dumps({
                    "artifact_root": "Fabric",
                    "pipelines_root": "Fabric/Pipelines",
                }),
                encoding="utf-8",
            )

            _, _, rows_by_layer, stats, _ = parser.parse_repository(repo)
            rows = rows_by_layer["pipelines"]

            self.assertEqual(8, len(rows))
            self.assertEqual(1, stats["pipeline_lookup_rows"])
            self.assertEqual(1, stats["pipeline_copy_rows"])
            data_flow = next(
                row
                for row in rows
                if row["Source_Object"] == "@item().TableName"
                and row["Dependency_Type"] == "DATA_FLOW"
            )
            self.assertEqual("customers/@item().TableName", data_flow["Target_Object"])
            self.assertEqual("Yes", data_flow["Is_Ambiguous"])
            notebook_edge = next(
                row
                for row in rows
                if row["Source_Type"] == "PIPELINE"
                and row["Target_Type"] == "NOTEBOOK"
            )
            self.assertEqual("Orchestrate Customers", notebook_edge["Source_Object"])
            self.assertEqual("Quality Check", notebook_edge["Target_Object"])
            self.assertEqual("No", notebook_edge["Is_Ambiguous"])
            dependency_edge = next(
                row
                for row in rows
                if row["Source_Object"] == "Load each"
                and row["Target_Object"] == "Quality Check"
            )
            self.assertEqual("Completed", dependency_edge["Column_Or_Parameter"])

    def test_empty_pipeline_is_recognized_without_inventing_rows(self):
        parser = load_parser()
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory)
            content = repo / "Empty.DataPipeline" / "pipeline-content.json"
            content.parent.mkdir()
            content.write_text(
                json.dumps({"properties": {"activities": []}}),
                encoding="utf-8",
            )

            _, discovered, rows_by_layer, stats, _ = parser.parse_repository(repo)

            self.assertEqual(1, discovered["pipelines"])
            self.assertEqual([], rows_by_layer["pipelines"])
            self.assertEqual(1, stats["pipelines"])
            self.assertEqual(0, stats["pipeline_activities"])


if __name__ == "__main__":
    unittest.main()