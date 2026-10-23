#!/usr/bin/env python3
"""
parse_powerbi_lineage.py
────────────────────────
Reads the pre-built Power BI lineage CSV (lineage_metadata_powerbi.csv),
validates every row against the 19-column schema, enforces correct platform
tokens for POWER_BI_MODEL / POWER_BI_REPORT nodes, deduplicates, and writes
the cleaned output back to lineage_metadata_powerbi.csv.

Run:
    python "silver_agent/agents/lineage-viewer/tools/parsers/parse_powerbi_lineage.py"

Output: silver_agent/build/lineage-viewer/lineage-output/lineage_metadata_powerbi.csv

Schema (19 columns):
  Source_Schema, Source_Object, Source_Type,
  Target_Schema, Target_Object, Target_Type,
  Dependency_Type, Column_Or_Parameter, Schema_Bound,
  External_Location, Is_Ambiguous, Location_Type,
  Source_Platform, Target_Platform,
  Transform_Tool, Transform_Name, Transform_Path,
  Source_Column, Source_Parser
"""

import csv
import re
from pathlib import Path

# ─────────────────────────── Paths ───────────────────────────
WORKSPACE   = Path(__file__).resolve().parents[5]   # .../silver_agent/agents/lineage-viewer/tools/parsers -> repo root
OUTPUT_DIR  = Path(__file__).resolve().parents[4] / "build" / "lineage-viewer" / "lineage-output"   # .../silver_agent/build/lineage-viewer/lineage-output
INPUT_FILE  = OUTPUT_DIR / "lineage_metadata_powerbi.csv"
OUTPUT_FILE = INPUT_FILE   # overwrite in place

CSV_HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column", "Source_Parser",
]
LINEAGE_HEADER = CSV_HEADER[:-1]

# ─────────────── Platform normalisation rules ───────────────
# Driven by schema so the viewer renders distinct node colours:
#   POWER_BI_MODEL   →  amber  (model/semantic layer tables)
#   POWER_BI_REPORT  →  orange (published reports)
#   POWER_BI         →  yellow (generic / legacy fallback)

def _norm_platform(schema: str, declared: str) -> str:
    """Return the canonical platform token for a node."""
    s = (schema or "").strip().upper()
    if s == "POWER_BI_MODEL":
        return "POWER_BI_MODEL"
    if s == "POWER_BI_REPORTS":
        return "POWER_BI_REPORT"
    # Keep declared value for all other platforms (REDSHIFT, FILE, etc.)
    return (declared or "POWER_BI").strip().upper()


def dedup_key(row: dict) -> tuple:
    return (
        row.get("Source_Schema", ""),
        row.get("Source_Object", ""),
        row.get("Source_Type", ""),
        row.get("Target_Schema", ""),
        row.get("Target_Object", ""),
        row.get("Target_Type", ""),
        row.get("Column_Or_Parameter", ""),
        row.get("Source_Platform", ""),
        row.get("Target_Platform", ""),
        row.get("Transform_Name", ""),
        row.get("Transform_Path", ""),
        row.get("Source_Column", ""),
    )


def normalize_powerbi_rows(raw_rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Normalize pre-generated Power BI lineage rows without writing files."""
    seen: set = set()
    out_rows: list = []
    skipped_null = 0
    skipped_dup = 0
    fixed_plat = 0

    for raw_row in raw_rows:
        row = dict(raw_row)
        src_obj = (row.get("Source_Object") or "").strip()
        tgt_obj = (row.get("Target_Object") or "").strip()
        if not src_obj or src_obj == "NULL" or not tgt_obj or tgt_obj == "NULL":
            skipped_null += 1
            continue

        new_src_plat = _norm_platform(
            row.get("Source_Schema", ""), row.get("Source_Platform", "")
        )
        new_tgt_plat = _norm_platform(
            row.get("Target_Schema", ""), row.get("Target_Platform", "")
        )
        if new_src_plat != row.get("Source_Platform", ""):
            row["Source_Platform"] = new_src_plat
            fixed_plat += 1
        if new_tgt_plat != row.get("Target_Platform", ""):
            row["Target_Platform"] = new_tgt_plat
            fixed_plat += 1

        for column in LINEAGE_HEADER:
            if column not in row:
                row[column] = "NULL"

        key = dedup_key(row)
        if key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        out_rows.append({column: row[column] for column in LINEAGE_HEADER})

    return out_rows, {
        "input_rows": len(raw_rows),
        "skipped_null": skipped_null,
        "skipped_duplicates": skipped_dup,
        "platform_tokens_corrected": fixed_plat,
    }


def parse_powerbi():
    if not INPUT_FILE.exists():
        print(f"✗  Input not found: {INPUT_FILE}")
        print("   Run the Power BI lineage generation first.")
        return

    print(f"Reading  {INPUT_FILE} …", end=" ", flush=True)
    with open(INPUT_FILE, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        raw_rows = list(reader)
    print(f"{len(raw_rows)} rows")

    out_rows, stats = normalize_powerbi_rows(raw_rows)

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {**row, "Source_Parser": "POWER_BI"}
            for row in out_rows
        )

    # ── Summary ──────────────────────────────────────────────
    hop1_count = sum(
        1 for r in out_rows if r.get("Target_Schema") == "POWER_BI_MODEL"
    )
    hop2_count = sum(
        1 for r in out_rows if r.get("Target_Schema") == "POWER_BI_REPORTS"
    )
    report_count = len({
        r.get("Target_Object")
        for r in out_rows
        if r.get("Target_Schema") == "POWER_BI_REPORTS"
    })
    model_table_count = len({
        r.get("Target_Object")
        for r in out_rows
        if r.get("Target_Schema") == "POWER_BI_MODEL"
    })

    print()
    print("=" * 65)
    print("POWER BI LINEAGE PARSE SUMMARY")
    print("=" * 65)
    print(f"  Input rows:                 {stats['input_rows']:4d}")
    print(f"  Skipped (NULL object):      {stats['skipped_null']:4d}")
    print(f"  Skipped (duplicate):        {stats['skipped_duplicates']:4d}")
    print(f"  Platform tokens corrected:  {stats['platform_tokens_corrected']:4d}")
    print()
    print(f"  Output rows:                {len(out_rows):4d}")
    print(f"    Hop 1 (Source → Model):   {hop1_count:4d}")
    print(f"    Hop 2 (Model → Report):   {hop2_count:4d}")
    print()
    print(f"  Unique Power BI Reports:    {report_count:4d}")
    print(f"  Unique Model Tables:        {model_table_count:4d}")
    print()
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    parse_powerbi()
