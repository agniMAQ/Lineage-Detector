"""Markdown reporting for deterministic lineage runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from inventory import InventoryCandidate


@dataclass
class ParserExecutionReport:
    parser_id: str
    status: str
    assigned_files: int
    raw_rows: int = 0
    valid_rows: int = 0
    rejected_rows: int = 0
    duplicates_removed: int = 0
    output_file: Path | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    rejection_examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LlmPocExecutionReport:
    eligible_files: int
    requests: int
    raw_rows: int
    valid_rows: int
    rejected_rows: int
    unresolved_files: int
    skipped_files: int
    errors: int
    output_file: Path
    consolidated_file: Path
    consolidated_rows: int


@dataclass
class ExecutionReport:
    status: str
    input_folder: Path
    output_folder: Path
    files_scanned: int
    signature_counts: dict[str, int]
    parser_reports: list[ParserExecutionReport]
    unsupported: tuple[InventoryCandidate, ...]
    consolidated_rows: int
    consolidated_file: Path
    llm_poc: LlmPocExecutionReport | None = None
    llm_handled: tuple[InventoryCandidate, ...] = ()


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_execution_report(path: Path, report: ExecutionReport) -> None:
    lines = [
        "# Lineage Execution Report",
        "",
        "## Summary",
        "",
        f"- Status: **{report.status}**",
        f"- Input folder: `{report.input_folder}`",
        f"- Output folder: `{report.output_folder}`",
        f"- Files scanned: {report.files_scanned}",
        f"- Consolidated rows: {report.consolidated_rows}",
        f"- Consolidated CSV: `{report.consolidated_file}`",
        "",
        "## Inventory",
        "",
        "| Signature | Files |",
        "|---|---:|",
    ]
    if report.signature_counts:
        for signature, count in sorted(report.signature_counts.items()):
            lines.append(f"| {_cell(signature)} | {count} |")
    else:
        lines.append("| None | 0 |")

    lines.extend([
        "",
        "## Parser Results",
        "",
        "| Parser | Status | Assigned | Raw | Valid | Rejected | Duplicates | Output |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    if report.parser_reports:
        for parser_report in report.parser_reports:
            output = parser_report.output_file or "-"
            lines.append(
                f"| {_cell(parser_report.parser_id)} | {_cell(parser_report.status)} "
                f"| {parser_report.assigned_files} | {parser_report.raw_rows} "
                f"| {parser_report.valid_rows} | {parser_report.rejected_rows} "
                f"| {parser_report.duplicates_removed} | {_cell(output)} |"
            )
    else:
        lines.append("| None | FAILED | 0 | 0 | 0 | 0 | 0 | - |")

    if report.llm_poc is not None:
        poc = report.llm_poc
        lines.extend([
            "",
            "## LLM Pipeline POC",
            "",
            f"- Eligible files: {poc.eligible_files}",
            f"- Model requests: {poc.requests}",
            f"- Raw rows: {poc.raw_rows}",
            f"- Valid rows: {poc.valid_rows}",
            f"- Rejected rows: {poc.rejected_rows}",
            f"- Unresolved files: {poc.unresolved_files}",
            f"- Skipped files: {poc.skipped_files}",
            f"- Errors: {poc.errors}",
            f"- LLM POC CSV: `{poc.output_file}`",
            f"- Consolidated POC rows: {poc.consolidated_rows}",
            f"- Consolidated POC CSV: `{poc.consolidated_file}`",
        ])

    if report.llm_poc is not None:
        lines.extend(["", "## LLM Fallback Coverage", ""])
        if report.llm_handled:
            for candidate in report.llm_handled:
                lines.extend([
                    f"### `{candidate.relative_path}`",
                    "",
                    "- Status: Not covered by default parsers, used LLM",
                    "- Output validation: Passed.",
                    "",
                ])
        else:
            lines.append("No files have fully accepted LLM lineage output.")

    lines.extend(["", "## Unsupported Candidates", ""])
    if report.unsupported:
        for candidate in report.unsupported:
            lines.extend([
                f"### `{candidate.relative_path}`",
                "",
                f"- Detected signature: `{candidate.signature}`",
                f"- Reason: {candidate.evidence}",
                "",
            ])
    else:
        lines.append("None.")

    lines.extend(["", "## Diagnostics", ""])
    diagnostics_written = False
    for parser_report in report.parser_reports:
        if not (parser_report.error or parser_report.warnings or parser_report.rejection_examples):
            continue
        diagnostics_written = True
        lines.extend([f"### {parser_report.parser_id}", ""])
        if parser_report.error:
            lines.append(f"- Error: {_cell(parser_report.error)}")
        for warning in parser_report.warnings:
            lines.append(f"- Warning: {_cell(warning)}")
        for example in parser_report.rejection_examples:
            lines.append(f"- Rejected row: {_cell(example)}")
        lines.append("")
    if not diagnostics_written:
        lines.append("No parser errors, warnings, or rejected rows.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
