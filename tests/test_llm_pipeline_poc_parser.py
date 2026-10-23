from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from inventory import InventoryCandidate  # noqa: E402
from llm_pipeline_poc_parser import _config_from_environment, parse_candidates  # noqa: E402
from lineage_model import normalize_rows  # noqa: E402
from validation import (  # noqa: E402
    BOOLEAN_VALUES, DEPENDENCY_TYPES, LOCATION_TYPES, validate_rows,
)


ENVIRONMENT = {
    "LINEAGE_LLM_ENDPOINT": "https://llm.example.test/chat/completions",
    "LINEAGE_LLM_API_KEY": "test-key",
    "LINEAGE_LLM_MODEL": "test-model",
}


def canonical_row() -> dict[str, str]:
    return {
        "Source_Schema": "dbo",
        "Source_Object": "Customers",
        "Source_Type": "TABLE",
        "Target_Schema": "sales",
        "Target_Object": "Customers",
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


class LlmPipelinePocParserTests(unittest.TestCase):
    def test_http_remains_default(self):
        self.assertEqual("http", _config_from_environment(ENVIRONMENT).provider)

    def test_copilot_requires_its_own_model_but_no_http_credentials(self):
        config = _config_from_environment({
            "LINEAGE_LLM_PROVIDER": "copilot",
            "LINEAGE_COPILOT_MODEL": "sonnet-test",
            "LINEAGE_LLM_MODEL": "qwen-test",
        })
        self.assertEqual("copilot", config.provider)
        self.assertEqual("sonnet-test", config.model)
        self.assertEqual("", config.api_key)
        with self.assertRaisesRegex(ValueError, "LINEAGE_COPILOT_MODEL"):
            _config_from_environment({
                **ENVIRONMENT, "LINEAGE_LLM_PROVIDER": "copilot",
            })

    def test_invalid_provider_and_timeout_are_explicit_errors(self):
        for environment in [
            {**ENVIRONMENT, "LINEAGE_LLM_PROVIDER": "unknown"},
            *[
                {**ENVIRONMENT, "LINEAGE_LLM_TIMEOUT": timeout}
                for timeout in ("0", "-1", "nan", "inf", "invalid")
            ],
        ]:
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                _config_from_environment(environment)

    def test_copilot_routes_without_http_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "adf_pipeline.json"
            content = '{"activities": [{"type": "Copy"}]}'
            path.write_text(content, encoding="utf-8")
            candidate = InventoryCandidate(
                path, path.name, "unsupported-pipeline-definition", "Pipeline"
            )
            environment = {
                "LINEAGE_LLM_PROVIDER": "copilot",
                "LINEAGE_COPILOT_MODEL": "sonnet-test",
                "LINEAGE_LLM_TIMEOUT": "300",
            }
            with patch("copilot_backend.complete", return_value=json.dumps({
                "rows": [canonical_row()],
            })) as complete:
                result = parse_candidates(
                    root, (candidate,), environment=environment,
                    http_post=lambda *_: self.fail("HTTP must not be called"),
                )
            self.assertEqual(0, result.file_errors)
            self.assertEqual(1, result.requests)
            self.assertEqual("LLM_PIPELINE_POC", result.rows[0]["Transform_Tool"])
            self.assertEqual(path.name, result.rows[0]["Transform_Path"])
            self.assertEqual("Yes", result.rows[0]["Is_Ambiguous"])
            self.assertIn(content, complete.call_args.kwargs["user_prompt"])
            self.assertEqual("sonnet-test", complete.call_args.kwargs["model"])
            self.assertEqual(300, complete.call_args.kwargs["timeout_seconds"])
            self.assertIn(
                'For a Copy activity use Dependency_Type "DATA_FLOW"',
                complete.call_args.kwargs["user_prompt"],
            )
            self.assertIn("<row-enums>", complete.call_args.kwargs["user_prompt"])

            for response, error in [
                (None, RuntimeError("Copilot is not authenticated")),
                ("not-json", None),
            ]:
                with self.subTest(response=response), patch(
                    "copilot_backend.complete", return_value=response, side_effect=error
                ):
                    result = parse_candidates(root, (candidate,), environment=environment)
                self.assertEqual([], result.rows)
                self.assertEqual(1, result.file_errors)
                self.assertIn(path.name, result.warnings[0])

    def test_copilot_does_not_load_for_ineligible_candidates(self):
        with patch("copilot_backend.complete") as complete:
            result = parse_candidates(
                Path("."), (), environment={"LINEAGE_LLM_PROVIDER": "copilot"},
            )
        complete.assert_not_called()
        self.assertEqual(0, result.file_errors)

    def test_eligible_pipeline_returns_rows_with_forced_poc_provenance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "load_customers_pipeline.json"
            path.write_text('{"activities": [{"type": "Copy"}]}', encoding="utf-8")
            candidate = InventoryCandidate(
                path,
                path.name,
                "unsupported-pipeline-definition",
                "Pipeline-like structured document",
            )
            requests = []

            def fake_post(endpoint, api_key, payload, timeout_seconds):
                requests.append((endpoint, api_key, payload, timeout_seconds))
                return {
                    "choices": [{
                        "message": {"content": json.dumps({"rows": [canonical_row()]})},
                    }],
                }

            result = parse_candidates(
                root,
                (candidate,),
                environment=ENVIRONMENT,
                http_post=fake_post,
            )

            self.assertEqual(1, result.eligible_files)
            self.assertEqual(1, result.requests)
            self.assertEqual(0, result.file_errors)
            self.assertEqual(1, len(result.rows))
            self.assertEqual("LLM_PIPELINE_POC", result.rows[0]["Transform_Tool"])
            self.assertEqual("load_customers_pipeline.json", result.rows[0]["Transform_Path"])
            self.assertEqual("Yes", result.rows[0]["Is_Ambiguous"])
            self.assertEqual("test-model", requests[0][2]["model"])
            self.assertIn("<pipeline-file>", requests[0][2]["messages"][1]["content"])
            prompt = requests[0][2]["messages"][1]["content"]
            enums = json.loads(prompt.split("<row-enums>\n", 1)[1].split("\n</row-enums>", 1)[0])
            self.assertEqual({
                "Dependency_Type": sorted(DEPENDENCY_TYPES),
                "Location_Type": sorted(LOCATION_TYPES),
                "Schema_Bound": sorted(BOOLEAN_VALUES),
                "Is_Ambiguous": sorted(BOOLEAN_VALUES),
            }, enums)
            self.assertIn(
                'Location_Type "REMOTE", not "AZURE BLOB STORAGE"', prompt,
            )
            self.assertIn("exactly the columns above", prompt)

    def test_reported_invalid_enums_are_rejected_not_silently_coerced(self):
        row = canonical_row()
        row.update(Dependency_Type="COPY", Location_Type="AZURE BLOB STORAGE")
        result = validate_rows(normalize_rows([row] * 4))
        self.assertEqual([], result.valid_rows)
        self.assertEqual(4, len(result.rejected_rows))
        for rejected in result.rejected_rows:
            self.assertIn("unsupported Dependency_Type: COPY", rejected.reasons)
            self.assertIn("unsupported Location_Type: AZURE BLOB STORAGE", rejected.reasons)
        row.update(Dependency_Type="DATA_FLOW", Location_Type="REMOTE")
        corrected = validate_rows(normalize_rows([row]))
        self.assertEqual([], corrected.rejected_rows)
        self.assertEqual(1, len(corrected.valid_rows))

    def test_invalid_json_is_reported_without_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "pipeline.yaml"
            path.write_text("source: raw\ntarget: curated", encoding="utf-8")
            candidate = InventoryCandidate(
                path,
                path.name,
                "unsupported-pipeline-definition",
                "Pipeline-like structured document",
            )

            result = parse_candidates(
                root,
                (candidate,),
                environment=ENVIRONMENT,
                http_post=lambda *_: {
                    "choices": [{"message": {"content": "not-json"}}],
                },
            )

            self.assertEqual([], result.rows)
            self.assertEqual(1, result.file_errors)
            self.assertIn("pipeline.yaml", result.warnings[0])

    def test_missing_configuration_does_not_call_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "pipeline.json"
            path.write_text('{"activities": []}', encoding="utf-8")
            candidate = InventoryCandidate(
                path,
                path.name,
                "unsupported-pipeline-definition",
                "Pipeline-like structured document",
            )

            result = parse_candidates(
                root,
                (candidate,),
                environment={},
                http_post=lambda *_: self.fail("endpoint should not be called"),
            )

            self.assertEqual(0, result.requests)
            self.assertEqual(1, result.file_errors)
            self.assertIn("LINEAGE_LLM_ENDPOINT", result.warnings[0])

    def test_oversized_and_non_pipeline_candidates_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            oversized = root / "large_pipeline.json"
            oversized.write_text("x" * 11, encoding="utf-8")
            unrelated = root / "notes.json"
            unrelated.write_text("{}", encoding="utf-8")
            candidates = (
                InventoryCandidate(
                    oversized,
                    oversized.name,
                    "unsupported-pipeline-definition",
                    "Pipeline-like structured document",
                ),
                InventoryCandidate(
                    unrelated,
                    unrelated.name,
                    "unsupported-lineage-csv",
                    "Unsupported lineage input",
                ),
            )

            result = parse_candidates(
                root,
                candidates,
                environment=ENVIRONMENT,
                http_post=lambda *_: self.fail("endpoint should not be called"),
                max_file_bytes=10,
            )

            self.assertEqual(0, result.eligible_files)
            self.assertEqual(2, result.skipped_files)
            self.assertEqual(0, result.requests)


if __name__ == "__main__":
    unittest.main()