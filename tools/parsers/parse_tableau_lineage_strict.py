#!/usr/bin/env python3
"""
Strict Tableau lineage parser driven ONLY by CSV columns:
- Project
- Workbook
- Source System Type
- Table Name
- Source Table(s) Used

No Source Queries usage.
No fallback to Table Name when Source Table(s) Used is empty.
Flow: source table -> tableau table -> tableau report
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

WORKSPACE = Path(r"c:\Repos\WK LR US Artifacts")
INPUT_CSV = WORKSPACE / "csv" / "Tableau Workbook Analysis(Table Details).csv"
REPORT_INVENTORY_CSV = WORKSPACE / "csv" / "Power BI - Tableau Reports Consolidated(Report Inventory).csv"
OUTPUT_CSV = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "lineage_metadata_tableau.csv"
MISSING_CSV = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "tableau_missing_source_tables.csv"

HEADER = [
    "Source_Schema",
    "Source_Object",
    "Source_Type",
    "Target_Schema",
    "Target_Object",
    "Target_Type",
    "Dependency_Type",
    "Column_Or_Parameter",
    "Schema_Bound",
    "External_Location",
    "Is_Ambiguous",
    "Location_Type",
    "Source_Platform",
    "Target_Platform",
    "Transform_Tool",
    "Transform_Name",
    "Transform_Path",
    "Source_Column",
    "Source_Parser",
]
LINEAGE_HEADER = HEADER[:-1]

PLATFORM_MAP = {
    "amazon redshift": "REDSHIFT",
    "csv/text file": "FILE",
    "excel file": "FILE",
    "tableau hyper extract": "FILE",
    "amazon athena": "ATHENA",
    "oracle db": "ORACLE",
    "salesforce": "SALESFORCE",
    "amazon aurora (aws)": "AURORA",
    "google analytics": "GOOGLE_ANALYTICS",
}


def clean(v: str) -> str:
    return (v or "").strip()


def norm_key(v: str) -> str:
    """Normalize report key text for robust exact-match filtering."""
    s = clean(v)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def read_csv_with_fallback(path: Path) -> List[Dict[str, str]]:
    """Read CSV trying common encodings used in inventory exports."""
    encodings = ["utf-8-sig", "cp1252", "latin-1"]
    last_err: Exception | None = None
    for enc in encodings:
        try:
            with path.open("r", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise RuntimeError(f"Unable to read CSV with fallback encodings: {path}") from last_err


def load_in_scope_tableau_reports(
    report_inventory_csv: Path = REPORT_INVENTORY_CSV,
) -> Set[Tuple[str, str]]:
    """
    Build allowed (Workspace/Folder, Reports/Workbooks) keys from report inventory
    where Type=Tableau and Is in scope for migration?=Yes.
    """
    rows = read_csv_with_fallback(report_inventory_csv)
    keys: Set[Tuple[str, str]] = set()
    for row in rows:
        typ = norm_key(row.get("Type", ""))
        scope = norm_key(row.get("Is in scope for migration? (Yes/No)", ""))
        if typ != "tableau" or scope != "yes":
            continue
        workspace = norm_key(row.get("Workspace/Folder", ""))
        workbook = norm_key(row.get("Reports/Workbooks", ""))
        if workspace and workbook:
            keys.add((workspace, workbook))
    return keys


def map_platform(source_system_type: str) -> str:
    s = clean(source_system_type).lower()
    for key, token in PLATFORM_MAP.items():
        if key in s:
            return token
    return "REDSHIFT"


def map_location_type(source_system_type: str) -> str:
    s = clean(source_system_type).lower()
    if "csv/text file" in s or "excel file" in s or "tableau hyper extract" in s:
        return "LOCAL"
    return "EXTERNAL"


def split_schema_object(name: str) -> Tuple[str, str]:
    n = clean(name).strip('"').strip("'")
    if not n:
        return "NULL", "NULL"
    if "." in n:
        left, right = n.split(".", 1)
        return clean(left).lower() or "NULL", clean(right).lower() or "NULL"
    return "NULL", n.lower()


def infer_source_type(source_object: str, source_system_type: str) -> str:
    s = clean(source_system_type).lower()
    o = clean(source_object).lower()
    if "csv/text file" in s or "excel file" in s or "tableau hyper extract" in s:
        return "FILE"
    if o.startswith("vw_") or o.startswith("v_") or ".vw_" in o or ".v_" in o:
        return "VIEW"
    return "TABLE"


def external_location(row: Dict[str, str]) -> str:
    host = clean(row.get("Server / Host Address"))
    db = clean(row.get("Database"))
    if host and db:
        return f"{host}:{db}"
    if host:
        return host
    return "NULL"


def extract_sources_from_used(row: Dict[str, str]) -> List[str]:
    raw = clean(row.get("Source Table(s) Used"))
    if not raw:
        return []

    # Use Source Queries only to identify CTE aliases that must be excluded.
    source_query = clean(row.get("Source Queries"))
    cte_aliases = extract_cte_aliases(source_query)

    out: List[str] = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        schema_name, object_name = split_schema_object(item)
        # Exclude unqualified CTE aliases such as WITH foo AS (...)
        if schema_name == "NULL" and object_name.lower() in cte_aliases:
            continue
        out.append(item)
    return out


def extract_cte_aliases(sql_text: str) -> Set[str]:
    if not sql_text:
        return set()
    # Matches aliases in patterns: WITH cte AS (...) or , cte AS (...)
    pattern = re.compile(r"(?is)(?:with|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(")
    return {m.lower() for m in pattern.findall(sql_text)}


def extract_sources_from_query(sql_text: str) -> List[str]:
    sql = clean(sql_text)
    if not sql:
        return []

    cte_aliases = extract_cte_aliases(sql)
    # Capture table-like tokens after FROM/JOIN.
    tokens = re.findall(r"(?is)\b(?:from|join)\s+([a-zA-Z0-9_\.\"`]+)", sql)

    out: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        t = clean(token).strip('"').strip("'").strip("`")
        if not t or t.startswith("("):
            continue
        # For Redshift query parsing, keep fully-qualified objects to avoid aliases.
        if "." not in t:
            continue
        schema_name, object_name = split_schema_object(t)
        if schema_name == "NULL" and object_name.lower() in cte_aliases:
            continue
        key = f"{schema_name}.{object_name}" if schema_name != "NULL" else object_name
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def make_hop1(
    table_key: str,
    report_key: str,
    table_name: str,
    transform_path: str,
) -> List[str]:
    return [
        "TABLEAU_MODEL",
        table_key,
        "TABLE",
        "TABLEAU_REPORTS",
        report_key,
        "TABLEAU_REPORT",
        "DATA_FLOW",
        "*",
        "No",
        "NULL",
        "No",
        "LOCAL",
        "TABLEAU",
        "TABLEAU",
        "None",
        table_name,
        transform_path,
        "NULL",
        "TABLEAU",
    ]


def make_hop2(
    source_schema: str,
    source_object: str,
    source_type: str,
    source_platform: str,
    location_type: str,
    ext_location: str,
    table_key: str,
    table_name: str,
    transform_path: str,
) -> List[str]:
    return [
        source_schema,
        source_object,
        source_type,
        "TABLEAU_MODEL",
        table_key,
        "TABLE",
        "DATA_FLOW",
        "*",
        "No",
        ext_location,
        "No",
        location_type,
        source_platform,
        "TABLEAU",
        "None",
        table_name,
        transform_path,
        "NULL",
        "TABLEAU",
    ]


def parse_rows(
    input_csv: Path,
    report_inventory_csv: Path | None = None,
    root_path: Path | None = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, int]]:
    """Parse supported Tableau metadata CSV rows without writing files."""
    rows_out: List[Dict[str, str]] = []
    missing_rows: List[Dict[str, str]] = []
    seen: Set[Tuple[str, ...]] = set()
    in_scope = (
        load_in_scope_tableau_reports(report_inventory_csv)
        if report_inventory_csv is not None
        else None
    )
    matched_reports: Set[Tuple[str, str]] = set()
    skipped_out_of_scope = 0
    try:
        transform_path = str(input_csv.relative_to(root_path)).replace("\\", "/") if root_path else input_csv.name
    except ValueError:
        transform_path = str(input_csv).replace("\\", "/")

    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            project = clean(row.get("Project"))
            workbook = clean(row.get("Workbook")) or "UNKNOWN_TABLEAU_REPORT"

            # Filter to in-scope Tableau reports from report inventory.
            report_filter_key = (norm_key(project), norm_key(workbook))
            if in_scope is not None and report_filter_key not in in_scope:
                skipped_out_of_scope += 1
                continue
            matched_reports.add(report_filter_key)

            report_key = f"{project}.{workbook}" if project else workbook

            table_name = clean(row.get("Table Name")) or "UNKNOWN_TABLE"
            table_key = f"{report_key}::{table_name}"

            source_system_type = clean(row.get("Source System Type"))
            source_platform = map_platform(source_system_type)
            location_type = map_location_type(source_system_type)
            ext_location = external_location(row)

            hop1 = make_hop1(table_key, report_key, table_name, transform_path)
            k1 = (hop1[0], hop1[1].lower(), hop1[3], hop1[4].lower(), hop1[12], hop1[13], hop1[15])
            if k1 not in seen:
                seen.add(k1)
                rows_out.append(dict(zip(LINEAGE_HEADER, hop1[:18])))

            if source_platform == "REDSHIFT":
                # For Redshift, prefer Source Queries; fallback to Source Table(s) Used when empty.
                sources = extract_sources_from_query(row.get("Source Queries"))
                if not sources:
                    sources = extract_sources_from_used(row)
            else:
                sources = extract_sources_from_used(row)
            if not sources:
                missing_rows.append(
                    {
                        "Project": project,
                        "Workbook": workbook,
                        "Table Name": table_name,
                        "Source System Type": source_system_type,
                        "Reason": "Source Table(s) Used is empty",
                    }
                )
                continue

            for src in sources:
                src_schema, src_object = split_schema_object(src)
                src_type = infer_source_type(src_object, source_system_type)

                hop2 = make_hop2(
                    source_schema=src_schema,
                    source_object=src_object,
                    source_type=src_type,
                    source_platform=source_platform,
                    location_type=location_type,
                    ext_location=ext_location,
                    table_key=table_key,
                    table_name=table_name,
                    transform_path=transform_path,
                )
                k2 = (hop2[0], hop2[1].lower(), hop2[3], hop2[4].lower(), hop2[12], hop2[13], hop2[15])
                if k2 in seen:
                    continue
                seen.add(k2)
                rows_out.append(dict(zip(LINEAGE_HEADER, hop2[:18])))

    stats = {
        "rows": len(rows_out),
        "missing_sources": len(missing_rows),
        "in_scope_reports": len(in_scope) if in_scope is not None else 0,
        "matched_reports": len(matched_reports),
        "skipped_out_of_scope": skipped_out_of_scope,
    }
    return rows_out, missing_rows, stats


def parse_tableau() -> None:
    rows_out, missing_rows, stats = parse_rows(INPUT_CSV, REPORT_INVENTORY_CSV)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(
            {**row, "Source_Parser": "TABLEAU"}
            for row in rows_out
        )

    with MISSING_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["Project", "Workbook", "Table Name", "Source System Type", "Reason"],
        )
        w.writeheader()
        w.writerows(missing_rows)

    print(f"Created: {OUTPUT_CSV}")
    print(f"Rows: {stats['rows']}")
    print(f"Missing Source Table(s) Used rows: {stats['missing_sources']}")
    print(f"Missing report: {MISSING_CSV}")
    print(f"In-scope Tableau reports configured: {stats['in_scope_reports']}")
    print(f"In-scope Tableau reports matched: {stats['matched_reports']}")
    print(f"Out-of-scope rows skipped: {stats['skipped_out_of_scope']}")
    if stats["in_scope_reports"] != 204:
        print(
            "WARN: Expected 204 in-scope Tableau reports but found "
            f"{stats['in_scope_reports']} in report inventory filter."
        )


if __name__ == "__main__":
    parse_tableau()
