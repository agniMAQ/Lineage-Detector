#!/usr/bin/env python3
"""Deterministic multi-platform lineage command-line interface."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from deduplication import deduplicate_rows
from inventory import build_inventory
import llm_pipeline_poc_parser
from lineage_model import LINEAGE_COLUMNS, normalize_rows
from parser_adapters import ParserContext
from parser_registry import PARSER_REGISTRY, ParserSpec, select_parsers
from reporting import (
    ExecutionReport,
    LlmPocExecutionReport,
    ParserExecutionReport,
    write_execution_report,
)
from validation import validate_rows


CONSOLIDATED_FILENAME = "lineage_metadata_consolidated.csv"
REPORT_FILENAME = "lineage_execution_report.md"
LLM_POC_FILENAME = "lineage_metadata_llm_pipeline_poc.csv"
POC_CONSOLIDATED_FILENAME = "lineage_metadata_consolidated_poc.csv"


@dataclass(frozen=True)
class RunResult:
    status: str
    consolidated_file: Path
    report_file: Path
    consolidated_rows: int
    poc_consolidated_file: Path | None = None
    poc_consolidated_rows: int = 0


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_rows = sorted(
        rows,
        key=lambda row: tuple(row.get(column, "").casefold() for column in LINEAGE_COLUMNS),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LINEAGE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered_rows)


def _parser_status(stats: dict[str, object]) -> str:
    try:
        return "PARTIAL" if int(stats.get("file_errors", 0)) > 0 else "SUCCESS"
    except (TypeError, ValueError):
        return "SUCCESS"


def _determine_status(
    consolidated_rows: list[dict[str, str]],
    parser_reports: list[ParserExecutionReport],
    unsupported_count: int,
) -> str:
    if not consolidated_rows or not parser_reports:
        return "FAILED"
    if unsupported_count or any(report.status != "SUCCESS" for report in parser_reports):
        return "PARTIAL"
    return "SUCCESS"


def run_generate(
    input_folder: Path,
    output_folder: Path | None = None,
    registry: tuple[ParserSpec, ...] = PARSER_REGISTRY,
    llm_poc: bool = False,
) -> RunResult:
    root = input_folder.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Input folder does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Input path is not a folder: {root}")

    output = (output_folder or (root / "lineage-output")).expanduser().resolve()
    if output == root:
        raise ValueError("Output folder cannot be the input repository root")

    inventory = build_inventory(root, (output,))
    selection = select_parsers(inventory, registry)
    registry_lookup = {spec.parser_id: spec for spec in registry}
    context = ParserContext(root)
    parser_reports: list[ParserExecutionReport] = []
    all_valid_rows: list[dict[str, str]] = []

    output.mkdir(parents=True, exist_ok=True)
    for spec in registry:
        candidates = selection.assignments.get(spec.parser_id)
        if not candidates:
            continue
        parser_report = ParserExecutionReport(
            parser_id=spec.parser_id,
            status="SUCCESS",
            assigned_files=len(candidates),
        )
        try:
            adapter_result = registry_lookup[spec.parser_id].adapter(context, candidates)
            parser_report.raw_rows = len(adapter_result.rows)
            parser_report.warnings.extend(adapter_result.warnings)

            normalized_rows = normalize_rows(adapter_result.rows)
            validation_result = validate_rows(normalized_rows)
            parser_report.rejected_rows = len(validation_result.rejected_rows)
            parser_report.warnings.extend(validation_result.warnings)
            parser_report.rejection_examples.extend(
                "; ".join(rejected.reasons)
                for rejected in validation_result.rejected_rows[:10]
            )
            unique_rows, removed = deduplicate_rows(validation_result.valid_rows)
            parser_report.valid_rows = len(unique_rows)
            parser_report.duplicates_removed = removed
            parser_report.status = _parser_status(adapter_result.stats)
            parser_output = output / spec.output_filename
            _write_csv(parser_output, unique_rows)
            parser_report.output_file = parser_output
            all_valid_rows.extend(unique_rows)
        except Exception as exc:
            parser_report.status = "FAILED"
            parser_report.error = f"{type(exc).__name__}: {exc}"
        parser_reports.append(parser_report)

    consolidated_rows, _ = deduplicate_rows(all_valid_rows)
    consolidated_file = output / CONSOLIDATED_FILENAME
    _write_csv(consolidated_file, consolidated_rows)
    poc_consolidated_file: Path | None = None
    poc_consolidated_rows: list[dict[str, str]] = []
    llm_poc_report: LlmPocExecutionReport | None = None
    llm_handled = ()
    remaining_unsupported = selection.unsupported

    if llm_poc:
        poc_result = llm_pipeline_poc_parser.parse_candidates(root, selection.unsupported)
        poc_parser_report = ParserExecutionReport(
            parser_id="llm_pipeline_poc",
            status="SUCCESS",
            assigned_files=poc_result.eligible_files,
            raw_rows=len(poc_result.rows),
            warnings=list(poc_result.warnings),
        )
        normalized_poc_rows = normalize_rows(poc_result.rows)
        poc_validation = validate_rows(normalized_poc_rows)
        poc_parser_report.rejected_rows = len(poc_validation.rejected_rows)
        poc_parser_report.warnings.extend(poc_validation.warnings)
        poc_parser_report.rejection_examples.extend(
            "; ".join(rejected.reasons)
            for rejected in poc_validation.rejected_rows[:10]
        )
        accepted_paths = {row["Transform_Path"] for row in poc_validation.valid_rows}
        rejected_paths = {
            rejected.row.get("Transform_Path")
            for rejected in poc_validation.rejected_rows
        }
        handled_paths = accepted_paths - rejected_paths
        llm_handled = tuple(
            candidate for candidate in selection.unsupported
            if candidate.relative_path in handled_paths
        )
        remaining_unsupported = tuple(
            candidate for candidate in selection.unsupported
            if candidate.relative_path not in handled_paths
        )
        unique_poc_rows, poc_duplicates = deduplicate_rows(poc_validation.valid_rows)
        poc_parser_report.valid_rows = len(unique_poc_rows)
        poc_parser_report.duplicates_removed = poc_duplicates
        if poc_result.file_errors or poc_parser_report.rejected_rows:
            poc_parser_report.status = "PARTIAL"

        poc_output_file = output / LLM_POC_FILENAME
        _write_csv(poc_output_file, unique_poc_rows)
        poc_parser_report.output_file = poc_output_file
        parser_reports.append(poc_parser_report)

        poc_consolidated_rows, _ = deduplicate_rows(consolidated_rows + unique_poc_rows)
        poc_consolidated_file = output / POC_CONSOLIDATED_FILENAME
        _write_csv(poc_consolidated_file, poc_consolidated_rows)
        llm_poc_report = LlmPocExecutionReport(
            eligible_files=poc_result.eligible_files,
            requests=poc_result.requests,
            raw_rows=len(poc_result.rows),
            valid_rows=len(unique_poc_rows),
            rejected_rows=poc_parser_report.rejected_rows,
            unresolved_files=poc_result.unresolved_files,
            skipped_files=poc_result.skipped_files,
            errors=poc_result.file_errors,
            output_file=poc_output_file,
            consolidated_file=poc_consolidated_file,
            consolidated_rows=len(poc_consolidated_rows),
        )

    rows_for_status = poc_consolidated_rows if llm_poc else consolidated_rows
    status = _determine_status(
        rows_for_status,
        parser_reports,
        len(remaining_unsupported),
    )
    report_file = output / REPORT_FILENAME
    write_execution_report(
        report_file,
        ExecutionReport(
            status=status,
            input_folder=root,
            output_folder=output,
            files_scanned=inventory.files_scanned,
            signature_counts=inventory.signature_counts,
            parser_reports=parser_reports,
            unsupported=remaining_unsupported,
            consolidated_rows=len(consolidated_rows),
            consolidated_file=consolidated_file,
            llm_poc=llm_poc_report,
            llm_handled=llm_handled,
        ),
    )
    return RunResult(
        status,
        consolidated_file,
        report_file,
        len(consolidated_rows),
        poc_consolidated_file,
        len(poc_consolidated_rows),
    )


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic multi-platform lineage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate", help="Generate lineage for a local folder")
    generate_parser.add_argument("input_folder", type=Path, help="Repository or project folder")
    generate_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder (default: <input-folder>/lineage-output)",
    )
    generate_parser.add_argument(
        "--llm-poc",
        action="store_true",
        help="Run the optional LLM POC for unsupported ADF JSON and pipeline YAML files",
    )
    return parser.parse_args(argv)


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Values already present in the environment take precedence, so a variable
    set in the terminal overrides the .env file. Lines that are blank or start
    with '#' are ignored, and surrounding quotes around a value are stripped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    _load_env_file(Path(__file__).resolve().parent.parent / ".env")
    args = parse_arguments(argv)
    try:
        result = run_generate(args.input_folder, args.output_dir, llm_poc=args.llm_poc)
    except (FileNotFoundError, NotADirectoryError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Status:           {result.status}")
    print(f"Consolidated CSV: {result.consolidated_file}")
    print(f"Execution report: {result.report_file}")
    print(f"Rows:             {result.consolidated_rows}")
    if result.poc_consolidated_file is not None:
        print(f"POC consolidated: {result.poc_consolidated_file}")
        print(f"POC rows:         {result.poc_consolidated_rows}")
    return {"SUCCESS": 0, "PARTIAL": 1, "FAILED": 2}[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
