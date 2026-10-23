"""Deterministic, read-only inventory of lineage-related repository files."""

from __future__ import annotations

import csv
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lineage_model import LINEAGE_COLUMNS


EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".ipynb_checkpoints",
    "lineage-output",
}

UNSUPPORTED_LINEAGE_EXTENSIONS = {
    ".dtsx",
    ".kjb",
    ".ktr",
    ".pbix",
    ".twb",
    ".twbx",
}


@dataclass(frozen=True)
class InventoryCandidate:
    path: Path
    relative_path: str
    signature: str
    evidence: str
    likely_lineage: bool = True


@dataclass(frozen=True)
class Inventory:
    root: Path
    files_scanned: int
    excluded_directories: int
    candidates: tuple[InventoryCandidate, ...]
    signature_counts: dict[str, int]


def _relative_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _is_fabric_path(path: Path) -> bool:
    lower_parts = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    if any(
        part.endswith((
            ".notebook",
            ".semanticmodel",
            ".report",
            ".lakehouse",
            ".datapipeline",
        ))
        for part in lower_parts
    ):
        return True
    if name in {
        "definition.pbir",
        "shortcuts.metadata.json",
        "sources_config.json",
        "static_files_config.json",
        "redshift_direct_tables.json",
        "lineage.config.json",
    }:
        return True
    if name == "notebook-content.py":
        return "notebooks" in lower_parts or any(
            part in {"bronze", "silver", "gold"} for part in lower_parts
        )
    return path.suffix.casefold() == ".pbip"


def _read_prefix(path: Path, limit: int = 65536) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _csv_header(path: Path) -> set[str]:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return {str(value or "").strip() for value in next(csv.reader(handle), [])}
        except UnicodeDecodeError:
            continue
        except (OSError, csv.Error):
            return set()
    return set()


def _csv_source_parser(path: Path) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                row = next(csv.DictReader(handle), {})
                return str(row.get("Source_Parser") or "").strip().upper()
        except UnicodeDecodeError:
            continue
        except (OSError, csv.Error):
            return ""
    return ""


def _classify_csv(path: Path) -> tuple[str, str] | None:
    header = _csv_header(path)
    folded = {column.casefold() for column in header}
    if {
        "pipeline_name",
        "relative_item_path",
    }.issubset(folded) and ({"components", "source_components", "target_components"} & folded):
        return "talend-inventory", "Talend pipeline inventory columns"
    if {"relative_path", "subtype"}.issubset(folded):
        return "talend-call-analysis", "Talend call-analysis companion columns"
    if {
        "project",
        "workbook",
        "table name",
        "source system type",
    }.issubset(folded) and ({"source table(s) used", "source queries"} & folded):
        return "tableau-metadata", "Tableau workbook metadata columns"
    if {
        "type",
        "workspace/folder",
        "reports/workbooks",
        "is in scope for migration? (yes/no)",
    }.issubset(folded):
        return "tableau-report-inventory", "Tableau report inventory companion columns"
    if {
        "object_type",
        "object_definition",
        "object_name",
        "schema",
    }.issubset(folded):
        return "redshift-view-inventory", "Redshift object definition columns"
    if set(LINEAGE_COLUMNS).issubset(header):
        source_parser = _csv_source_parser(path) if "Source_Parser" in header else ""
        if "powerbi" in path.name.casefold() or source_parser in {"POWER_BI", "POWERBI"}:
            return "powerbi-lineage", "Canonical Power BI lineage columns"
        return "unsupported-lineage-csv", "Canonical lineage CSV has no approved input adapter"
    return None


def _looks_like_pipeline_json(path: Path) -> bool:
    if path.suffix.casefold() not in {".json", ".yaml", ".yml", ".xml"}:
        return False
    name = path.name.casefold()
    if any(token in name for token in ("pipeline", "workflow", "dataflow", "etl")):
        return True
    content = _read_prefix(path).casefold()
    return (
        '"activities"' in content
        or '"pipeline"' in content
        or ('"source"' in content and '"sink"' in content)
    )


def classify_file(path: Path, root: Path) -> InventoryCandidate | None:
    relative_path = _relative_path(path, root)
    suffix = path.suffix.casefold()
    if _is_fabric_path(path):
        return InventoryCandidate(path, relative_path, "fabric-artifact", "Fabric project structure")
    if suffix == ".py":
        return InventoryCandidate(path, relative_path, "python-source", "Python source file")
    if suffix == ".ipynb":
        return InventoryCandidate(path, relative_path, "python-notebook", "Jupyter notebook")
    if suffix in {".sql", ".ddl"}:
        return InventoryCandidate(path, relative_path, "standalone-sql", "SQL file extension")
    if suffix == ".txt":
        prefix = _read_prefix(path)
        if any(token in prefix.casefold() for token in ("create view", "create table", "insert into", "copy ")):
            return InventoryCandidate(path, relative_path, "standalone-sql", "SQL statement signature")
    if suffix == ".item":
        prefix = _read_prefix(path)
        if "componentName=" in prefix and ("<node" in prefix or "<TalendProperties" in prefix):
            return InventoryCandidate(path, relative_path, "talend-item", "Talend component XML")
        return InventoryCandidate(path, relative_path, "unsupported-item", "Unrecognized .item file")
    if suffix == ".csv":
        csv_match = _classify_csv(path)
        if csv_match:
            return InventoryCandidate(path, relative_path, csv_match[0], csv_match[1])
    if suffix in UNSUPPORTED_LINEAGE_EXTENSIONS:
        return InventoryCandidate(
            path,
            relative_path,
            "unsupported-binary-or-workbook",
            f"{suffix} lineage artifact has no approved parser",
        )
    if _looks_like_pipeline_json(path):
        return InventoryCandidate(
            path,
            relative_path,
            "unsupported-pipeline-definition",
            "Pipeline-like structured document",
        )
    return None


def build_inventory(root: Path, excluded_paths: tuple[Path, ...] = ()) -> Inventory:
    root = root.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Input folder does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Input path is not a folder: {root}")

    resolved_excluded_paths = {path.expanduser().resolve() for path in excluded_paths}
    candidates: list[InventoryCandidate] = []
    files_scanned = 0
    excluded_directories = 0
    for current_root, directory_names, file_names in os.walk(root):
        kept_directories = []
        for directory_name in directory_names:
            directory_path = (Path(current_root) / directory_name).resolve()
            if (
                directory_name.casefold() in EXCLUDED_DIRECTORY_NAMES
                or directory_path in resolved_excluded_paths
            ):
                excluded_directories += 1
            else:
                kept_directories.append(directory_name)
        directory_names[:] = sorted(kept_directories, key=str.casefold)

        current_path = Path(current_root)
        for file_name in sorted(file_names, key=str.casefold):
            path = current_path / file_name
            files_scanned += 1
            candidate = classify_file(path, root)
            if candidate is not None:
                candidates.append(candidate)

    candidates.sort(key=lambda candidate: candidate.relative_path.casefold())
    counts = Counter(candidate.signature for candidate in candidates)
    return Inventory(root, files_scanned, excluded_directories, tuple(candidates), dict(counts))
