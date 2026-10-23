"""In-memory adapters around the existing platform-specific parsers."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from inventory import InventoryCandidate
from parsers import parse_fabric_lineage
from parsers import parse_powerbi_lineage
from parsers import parse_python_lineage
from parsers import parse_redshift_lineage
from parsers import parse_tableau_lineage_strict
from parsers import parse_talend_csv_lineage
from parsers import parse_talend_inventory_lineage


@dataclass(frozen=True)
class ParserContext:
    root: Path


@dataclass
class AdapterResult:
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)


def parse_fabric(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    layout, discovered, rows_by_layer, parser_stats, resolver = (
        parse_fabric_lineage.parse_repository(context.root)
    )
    rows = [row for layer_rows in rows_by_layer.values() for row in layer_rows]
    warnings = list(layout.warnings)
    pipeline_warning_labels = {
        "pipelines_unreadable": "pipeline definition(s) could not be read",
        "pipelines_invalid_activities": "pipeline definition(s) had an invalid activities collection",
        "pipeline_copy_endpoints_unresolved": "Copy activity endpoint pair(s) could not be resolved",
        "pipeline_lookup_endpoints_unresolved": "Lookup source endpoint(s) could not be resolved",
        "pipeline_dependencies_unresolved": "activity dependency reference(s) could not be resolved",
        "pipeline_notebooks_unresolved": "notebook ID(s) had no exported notebook artifact; activity names were retained as ambiguous",
    }
    for metric, label in pipeline_warning_labels.items():
        count = int(parser_stats.get(metric, 0))
        if count:
            warnings.append(f"{count} {label}")
    pipeline_error_metrics = (
        "pipelines_unreadable",
        "pipelines_invalid_activities",
        "pipeline_copy_endpoints_unresolved",
        "pipeline_lookup_endpoints_unresolved",
        "pipeline_dependencies_unresolved",
    )
    stats: dict[str, object] = {
        "assigned_files": len(candidates),
        "emitted_rows": len(rows),
        "known_schemas": len(resolver.known_schemas),
        "file_errors": sum(int(parser_stats.get(metric, 0)) for metric in pipeline_error_metrics),
    }
    stats.update({f"discovered_{key}": value for key, value in discovered.items()})
    stats.update({f"parser_{key}": value for key, value in parser_stats.items()})
    return AdapterResult(rows, warnings, stats)


def parse_python(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    rows: list[dict] = []
    warnings: list[str] = []
    parsed_files = 0
    for candidate in candidates:
        try:
            file_rows = parse_python_lineage.parse_file(candidate.path, context.root)
            rows.extend(file_rows)
            parsed_files += 1
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")
    return AdapterResult(
        rows,
        warnings,
        {
            "assigned_files": len(candidates),
            "parsed_files": parsed_files,
            "file_errors": len(warnings),
            "emitted_rows": len(rows),
        },
    )


def _parse_sql_file(path: Path, root: Path) -> list[dict]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    content = parse_redshift_lineage.strip_sql_comments(content)
    rows: list[dict] = []
    for statement in (part.strip() for part in content.split(";")):
        if not statement or not parse_redshift_lineage.SQL_ANCHOR.search(statement):
            continue
        parsed_rows = parse_redshift_lineage.parse_statement(
            statement,
            path,
            path.stem,
            root,
        )
        rows.extend(
            dict(zip(parse_redshift_lineage.CSV_HEADER, parse_redshift_lineage.build_csv_row(row)))
            for row in parsed_rows
        )
    return rows


def _parse_redshift_inventory(path: Path, root: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        transform_path = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        transform_path = str(path).replace("\\", "/")
    for view_row in parse_redshift_lineage.load_view_definitions_from_inventory(path):
        statement = parse_redshift_lineage.strip_sql_comments(view_row["sql_text"])
        sources = parse_redshift_lineage.extract_sources(
            statement,
            parse_redshift_lineage.extract_ctes(statement),
        )
        for source in sources:
            parsed_row = {
                "source_schema": source["schema"],
                "source_object": source["object"],
                "source_type": "TABLE",
                "target_schema": view_row["schema"] or "public",
                "target_object": view_row["table_name"],
                "target_type": "VIEW",
                "transform_name": view_row["object_name"],
                "transform_path": transform_path,
            }
            rows.append(
                dict(
                    zip(
                        parse_redshift_lineage.CSV_HEADER,
                        parse_redshift_lineage.build_csv_row(parsed_row),
                    )
                )
            )
    return rows


def parse_sql(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    rows: list[dict] = []
    warnings: list[str] = []
    parsed_files = 0
    for candidate in candidates:
        try:
            if candidate.signature == "redshift-view-inventory":
                rows.extend(_parse_redshift_inventory(candidate.path, context.root))
            else:
                rows.extend(_parse_sql_file(candidate.path, context.root))
            parsed_files += 1
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")
    return AdapterResult(
        rows,
        warnings,
        {
            "assigned_files": len(candidates),
            "parsed_files": parsed_files,
            "file_errors": len(warnings),
            "emitted_rows": len(rows),
        },
    )


def parse_talend(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    rows: list[dict] = []
    warnings: list[str] = []
    item_candidates = [candidate for candidate in candidates if candidate.signature == "talend-item"]
    inventory_candidates = [
        candidate for candidate in candidates if candidate.signature == "talend-inventory"
    ]
    call_analysis = next(
        (
            candidate.path
            for candidate in candidates
            if candidate.signature == "talend-call-analysis"
        ),
        context.root / ".lineage-call-analysis-not-found.csv",
    )

    for candidate in item_candidates:
        try:
            rows.extend(parse_talend_csv_lineage.parse_item_file(candidate.path, context.root))
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")
    for candidate in inventory_candidates:
        try:
            rows.extend(
                parse_talend_inventory_lineage.build_rows(candidate.path, call_analysis)
            )
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")

    return AdapterResult(
        rows,
        warnings,
        {
            "assigned_files": len(candidates),
            "item_files": len(item_candidates),
            "inventory_files": len(inventory_candidates),
            "file_errors": len(warnings),
            "emitted_rows": len(rows),
        },
    )


def parse_tableau(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    rows: list[dict] = []
    warnings: list[str] = []
    metadata_candidates = [
        candidate for candidate in candidates if candidate.signature == "tableau-metadata"
    ]
    report_inventory = next(
        (
            candidate.path
            for candidate in candidates
            if candidate.signature == "tableau-report-inventory"
        ),
        None,
    )
    missing_sources = 0
    skipped_out_of_scope = 0
    for candidate in metadata_candidates:
        try:
            parsed_rows, missing_rows, stats = parse_tableau_lineage_strict.parse_rows(
                candidate.path,
                report_inventory,
                context.root,
            )
            rows.extend(parsed_rows)
            missing_sources += len(missing_rows)
            skipped_out_of_scope += int(stats["skipped_out_of_scope"])
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")
    if not metadata_candidates:
        warnings.append("No Tableau metadata CSV was assigned")
    return AdapterResult(
        rows,
        warnings,
        {
            "assigned_files": len(candidates),
            "metadata_files": len(metadata_candidates),
            "missing_sources": missing_sources,
            "skipped_out_of_scope": skipped_out_of_scope,
            "emitted_rows": len(rows),
        },
    )


def parse_powerbi(
    context: ParserContext,
    candidates: tuple[InventoryCandidate, ...],
) -> AdapterResult:
    rows: list[dict] = []
    warnings: list[str] = []
    corrected_platform_tokens = 0
    for candidate in candidates:
        try:
            with candidate.path.open("r", encoding="utf-8-sig", newline="") as handle:
                raw_rows = list(csv.DictReader(handle))
            normalized_rows, stats = parse_powerbi_lineage.normalize_powerbi_rows(raw_rows)
            rows.extend(normalized_rows)
            corrected_platform_tokens += stats["platform_tokens_corrected"]
        except Exception as exc:
            warnings.append(f"{candidate.relative_path}: {exc}")
    return AdapterResult(
        rows,
        warnings,
        {
            "assigned_files": len(candidates),
            "platform_tokens_corrected": corrected_platform_tokens,
            "file_errors": len(warnings),
            "emitted_rows": len(rows),
        },
    )
