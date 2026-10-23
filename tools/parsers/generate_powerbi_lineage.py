#!/usr/bin/env python3
"""
generate_powerbi_lineage.py
───────────────────────────
Reads the Power BI Consolidated Documentation CSV and produces a
3-hop lineage CSV:

  Redshift / File  →  POWER_BI_TABLE (physical query)
                   →  POWER_BI_MODEL  (semantic model, workspace-qualified)
                   →  POWER_BI_REPORTS (published report, workspace-qualified)

Output: silver_agent/build/lineage-viewer/lineage-output/lineage_metadata_powerbi.csv
"""

import csv
import re
from pathlib import Path

WORKSPACE   = Path(r"c:\Repos\WK LR US Artifacts")
SOURCE_CSV  = WORKSPACE / "csv" / "LR US - All Workspaces - Consolidated Documentation(Tables).csv"
CALC_SOURCE_CSV = WORKSPACE / "csv" / "LR US - All Workspaces - Consolidated Documentation(Calculated Tables).csv"
OUTPUT_DIR  = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output"
OUTPUT_FILE = OUTPUT_DIR / "lineage_metadata_powerbi.csv"

HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column", "Source_Parser",
]

REDSHIFT_HOST_NO_PORT = "lrus-bi-lake.cisvjbzvdcrw.us-east-2.redshift.amazonaws.com"
REDSHIFT_HOST_5439    = "lrus-bi-lake.cisvjbzvdcrw.us-east-2.redshift.amazonaws.com:5439"

seen: set  = set()
out:  list = []


def dk(r: dict) -> tuple:
    return (
        r["Source_Schema"], r["Source_Object"],
        r["Target_Schema"], r["Target_Object"],
    )


def add(r: dict) -> None:
    k = dk(r)
    if k not in seen:
        seen.add(k)
        out.append(r)


def base_row(**kw) -> dict:
    defaults = dict(
        Dependency_Type="DATA_FLOW", Column_Or_Parameter="*",
        Schema_Bound="No", External_Location="NULL",
        Is_Ambiguous="No", Location_Type="EXTERNAL",
        Source_Platform="REDSHIFT", Target_Platform="POWER_BI_TABLE",
        Transform_Tool="None", Transform_Name="NULL",
        Transform_Path="NULL", Source_Column="NULL",
        Source_Parser="POWER_BI",
    )
    defaults.update(kw)
    return defaults


def parse_source_tables(raw: str):
    """Split 'schema.table | schema.table' into list of (schema, table) tuples."""
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    result = []
    for p in parts:
        if "." in p:
            schema, table = p.split(".", 1)
            result.append((schema.strip().lower(), table.strip().lower()))
    return result


def parse_source_tables_fallback(raw: str):
    """
    More permissive table parser for Power BI source text.
    Handles raw strings that may contain one or more fully-qualified source tables.
    """
    sources = parse_source_tables(raw)
    if sources:
        return sources

    found = []
    for token in re.findall(r"([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)", raw or ""):
        found.append((token[0].lower(), token[1].lower()))
    return found


CALC_TABLE_ARG_FUNCTIONS = (
    "SELECTCOLUMNS",
    "ADDCOLUMNS",
    "FILTER",
    "CALCULATETABLE",
    "SUMMARIZE",
    "SUMMARIZECOLUMNS",
    "DISTINCT",
    "VALUES",
    "TOPN",
    "GROUPBY",
    "GENERATE",
    "NATURALINNERJOIN",
    "NATURALLEFTOUTERJOIN",
)

CALC_TABLE_REF_DENYLIST = {
    "UNION",
    "SELECTCOLUMNS",
    "ADDCOLUMNS",
    "FILTER",
    "CALCULATETABLE",
    "SUMMARIZE",
    "SUMMARIZECOLUMNS",
    "DISTINCT",
    "VALUES",
    "TOPN",
    "GROUPBY",
    "GENERATE",
    "NATURALINNERJOIN",
    "NATURALLEFTOUTERJOIN",
    "DATATABLE",
}


def parse_calculated_table_sources(dax_expr: str):
    """
    Extract table references from a calculated-table DAX expression.

    The export does not provide a source table column, so we infer lineage from
    table-style references such as:
      SELECTCOLUMNS(red_flags_spend_usage_output, ...)
      'VL Usage Monthly Trend'[enterprise_name]
      Query3[enterprise_name__c]
    """
    expr = dax_expr or ""
    refs: list = []
    seen_refs: set = set()

    def push(ref: str) -> None:
        ref = (ref or "").strip()
        if not ref:
            return
        ref_upper = ref.upper()
        if ref_upper in CALC_TABLE_REF_DENYLIST:
            return
        # Guard against regex backtracking artifacts like "UNIO" from "UNION(".
        if any(token.startswith(ref_upper) for token in CALC_TABLE_REF_DENYLIST):
            return
        if ref not in seen_refs:
            seen_refs.add(ref)
            refs.append(ref)

    # First argument to common table functions, e.g. SELECTCOLUMNS(table, ...).
    fn_pattern = (
        r"\b(?:"
        + "|".join(CALC_TABLE_ARG_FUNCTIONS)
        + r")\s*\(\s*(?:'([^']+)'|([A-Za-z_][\w\.]*))(?=\s*(?:,|\)))"
    )
    for match in re.finditer(fn_pattern, expr, flags=re.IGNORECASE | re.DOTALL):
        push(match.group(1) or match.group(2))

    # Explicit table[column] references.
    for match in re.finditer(r"(?:'([^']+)'|([A-Za-z_][\w\.]*))\s*\[", expr):
        push(match.group(1) or match.group(2))

    return refs


def load_input_rows():
    rows = []

    print(f"Reading {SOURCE_CSV} …", flush=True)
    with open(SOURCE_CSV, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        rows.extend(list(reader))

    if CALC_SOURCE_CSV.exists():
        print(f"Reading {CALC_SOURCE_CSV} …", flush=True)
        with open(CALC_SOURCE_CSV, encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for calc_row in reader:
                rows.append({
                    "Workspace": calc_row.get("Workspace", ""),
                    "Semantic Model": calc_row.get("Semantic Model", ""),
                    "Report Connected": calc_row.get("Report Connected", ""),
                    "Table Name": calc_row.get("Table Name", ""),
                    "Table Type": "Calculated",
                    "Visible": "Yes",
                    "Storage Mode": "Import",
                    "Data Source Type": "",
                    "Server / Connection": "",
                    "Source Table": "",
                    "DAX Expression": calc_row.get("DAX Expression", ""),
                })

    return rows


def transform_path(workspace: str, report: str) -> str:
    return f"powerbi-service/{workspace}/{report}"


def qualified(workspace: str, name: str) -> str:
    """Return workspace-qualified object name (displayed as 'name' in viewer)."""
    return f"{workspace} | {name}"


# ── Main ───────────────────────────────────────────────────────────────
rows = load_input_rows()
print(f"  {len(rows)} rows found")

# Track unique (workspace, report) pairs for Hop-3 dedup
semantic_model_seen: set = set()

for row in rows:
    workspace  = row["Workspace"].strip()
    sem_model  = row["Semantic Model"].strip()
    report     = row["Report Connected"].strip()
    pbi_table  = row["Table Name"].strip()
    table_type = row["Table Type"].strip()
    server     = row.get("Server / Connection", "").strip()
    schema_db  = row.get("Schema / Database", "").strip().lower()
    src_raw    = row["Source Table"].strip()

    path = transform_path(workspace, report)
    q_model  = qualified(workspace, sem_model)   # "LR US - Prod | Report Name"
    q_report = qualified(workspace, report)

    # ── Hop 3: Semantic Model → Report (one per unique workspace+report) ──
    hop3_key = (workspace, report)
    if hop3_key not in semantic_model_seen:
        semantic_model_seen.add(hop3_key)
        add(base_row(
            Source_Schema="POWER_BI_MODEL",  Source_Object=q_model,
            Source_Type="TABLE",             Source_Platform="POWER_BI_MODEL",
            Target_Schema="POWER_BI_REPORTS",Target_Object=q_report,
            Target_Type="POWER_BI_REPORT",   Target_Platform="POWER_BI_REPORT",
            External_Location="NULL",        Location_Type="LOCAL",
            Transform_Name=q_report,         Transform_Path=path,
        ))

    # ── Hop 2: PBI physical table → Semantic Model ────────────────────────
    add(base_row(
        Source_Schema="POWER_BI_TABLE", Source_Object=pbi_table,
        Source_Type="TABLE",            Source_Platform="POWER_BI_TABLE",
        Target_Schema="POWER_BI_MODEL", Target_Object=q_model,
        Target_Type="TABLE",            Target_Platform="POWER_BI_MODEL",
        External_Location="NULL",       Location_Type="LOCAL",
        Transform_Name=q_model,         Transform_Path=path,
    ))

    # ── Hop 1: Source → PBI physical table ────────────────────────────────
    if table_type == "Calculated":
        dax_expr = row.get("DAX Expression", "")
        calc_sources = parse_calculated_table_sources(dax_expr)
        for source_table in calc_sources:
            add(base_row(
                Source_Schema="POWER_BI_TABLE", Source_Object=source_table,
                Source_Type="TABLE",            Source_Platform="POWER_BI_TABLE",
                Target_Schema="POWER_BI_TABLE", Target_Object=pbi_table,
                Target_Type="TABLE",            Target_Platform="POWER_BI_TABLE",
                External_Location="NULL",       Location_Type="LOCAL",
                Transform_Name=pbi_table,        Transform_Path=path,
            ))
        continue

    elif (
        table_type == "Derived"
        or row.get("Data Source Type", "").strip() == "Derived"
        or "(from:" in src_raw.lower()
    ):
        # Extract "(from: source_table)" from src_raw for intra-model lineage.
        m = re.search(r"\(from:\s*(.+?)\)", src_raw, flags=re.IGNORECASE)
        source_pbi = m.group(1).strip() if m else src_raw
        add(base_row(
            Source_Schema="POWER_BI_TABLE", Source_Object=source_pbi,
            Source_Type="TABLE",            Source_Platform="POWER_BI_TABLE",
            Target_Schema="POWER_BI_TABLE", Target_Object=pbi_table,
            Target_Type="TABLE",            Target_Platform="POWER_BI_TABLE",
            External_Location="NULL",       Location_Type="LOCAL",
            Transform_Name=pbi_table,       Transform_Path=path,
        ))

    elif table_type in ("Json", "Embedded") or row["Data Source Type"].strip() == "Json":
        add(base_row(
            Source_Schema="NULL",          Source_Object=pbi_table,
            Source_Type="FILE",            Source_Platform="FILE",
            Target_Schema="POWER_BI_TABLE",Target_Object=pbi_table,
            Target_Type="TABLE",           Target_Platform="POWER_BI_TABLE",
            External_Location="NULL",      Location_Type="LOCAL",
            Transform_Name=pbi_table,      Transform_Path=path,
        ))

    elif row["Data Source Type"].strip() == "AmazonRedshift":
        rs_tables = parse_source_tables_fallback(src_raw)
        if not rs_tables and src_raw:
            # When source is unqualified (e.g. sf_user), use Schema / Database as schema.
            rs_schema = schema_db if schema_db else "NULL"
            rs_tables = [(rs_schema, src_raw.lower())]
        for rs_schema, rs_table in rs_tables:
            add(base_row(
                Source_Schema=rs_schema,       Source_Object=rs_table,
                Source_Type="TABLE",           Source_Platform="REDSHIFT",
                Target_Schema="POWER_BI_TABLE",Target_Object=pbi_table,
                Target_Type="TABLE",           Target_Platform="POWER_BI_TABLE",
                External_Location=server,      Location_Type="EXTERNAL",
                Transform_Name=pbi_table,      Transform_Path=path,
            ))

    else:
        # Fallback: skip or treat as ambiguous
        continue

# ── Write output ───────────────────────────────────────────────────────
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
    w.writeheader()
    w.writerows(out)

h1 = sum(1 for r in out if r["Target_Schema"] == "POWER_BI_TABLE")
h2 = sum(1 for r in out if r["Target_Schema"] == "POWER_BI_MODEL")
h3 = sum(1 for r in out if r["Target_Schema"] == "POWER_BI_REPORTS")
reports  = len({r["Target_Object"] for r in out if r["Target_Schema"] == "POWER_BI_REPORTS"})
sem_mods = len({r["Target_Object"] for r in out if r["Target_Schema"] == "POWER_BI_MODEL"})

print(f"\n{'='*60}")
print("POWER BI LINEAGE — 3-HOP GENERATION SUMMARY")
print(f"{'='*60}")
print(f"  Total rows:                {len(out):4d}")
print(f"  Hop 1 (→ PBI Table):      {h1:4d}")
print(f"  Hop 2 (→ Semantic Model):  {h2:4d}")
print(f"  Hop 3 (→ Report):          {h3:4d}")
print(f"  Unique Semantic Models:    {sem_mods:4d}")
print(f"  Unique Reports:            {reports:4d}")
print(f"  Output: {OUTPUT_FILE}")
