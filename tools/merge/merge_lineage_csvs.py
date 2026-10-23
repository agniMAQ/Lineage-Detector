#!/usr/bin/env python3
"""
Merge five lineage CSVs (Talend, Python, Redshift, Power BI, Tableau) into consolidated output.
Applies deduplication and tracks source parser for each row.
"""

import csv
from pathlib import Path
from collections import OrderedDict

OUTPUT_DIR = Path(__file__).resolve().parents[4] / "build" / "lineage-viewer" / "lineage-output"   # .../silver_agent/build/lineage-viewer/lineage-output
OUTPUT_FILE = OUTPUT_DIR / "lineage_metadata_consolidated.csv"

TALEND_CSV_LEGACY = OUTPUT_DIR / "lineage_metadata_talend_inventory.csv"
TALEND_CSV_INVENTORY = OUTPUT_DIR / "lineage_metadata_talend_inventory.csv"
TALEND_CSV_FALLBACK = OUTPUT_DIR / "lineage_metadata_talend.csv"
TALEND_CSV_FALLBACK_2 = OUTPUT_DIR / "lineage_metadata_talend_csv.csv"
PYTHON_CSV   = OUTPUT_DIR / "lineage_metadata_python.csv"
REDSHIFT_CSV = OUTPUT_DIR / "lineage_metadata_redshift.csv"
POWERBI_CSV  = OUTPUT_DIR / "lineage_metadata_powerbi.csv"
TABLEAU_CSV  = OUTPUT_DIR / "lineage_metadata_tableau.csv"


def resolve_talend_csv_path():
    """Pick the newest available Talend lineage CSV."""
    candidates = [
        p for p in (
            TALEND_CSV_LEGACY,
            TALEND_CSV_INVENTORY,
            TALEND_CSV_FALLBACK,
            TALEND_CSV_FALLBACK_2,
        ) if p.exists()
    ]
    if not candidates:
        raise FileNotFoundError(
            "No Talend lineage CSV found. Checked known names: "
            f"{TALEND_CSV_LEGACY}, {TALEND_CSV_INVENTORY}, {TALEND_CSV_FALLBACK}, {TALEND_CSV_FALLBACK_2}"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)

def normalize_path(path_str):
    """Normalize path for deduplication (remove version suffixes)."""
    if not path_str or path_str == "NULL":
        return path_str
    # Remove _\d+\.\d+ version suffix
    import re
    return re.sub(r'_\d+\.\d+(?=\.item|\.ipynb|\.txt|$)', '', path_str)

def dedup_key(row):
    """Generate 12-tuple deduplication key."""
    return (
        row.get('Source_Schema', ''),
        row.get('Source_Object', ''),
        row.get('Source_Type', ''),
        row.get('Target_Schema', ''),
        row.get('Target_Object', ''),
        row.get('Target_Type', ''),
        row.get('Column_Or_Parameter', ''),
        row.get('Source_Platform', ''),
        row.get('Target_Platform', ''),
        row.get('Transform_Name', ''),
        normalize_path(row.get('Transform_Path', '')),
        row.get('Source_Column', ''),
    )

def merge_csvs():
    """Merge all three lineage CSVs with deduplication."""
    
    all_rows = []
    source_map = {}  # key -> source parser
    talend_csv = resolve_talend_csv_path()
    
    # Load Talend CSV
    print(f"Loading Talend CSV ({talend_csv.name})...", end=" ")
    with open(talend_csv, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        talend_rows = list(reader)
    for row in talend_rows:
        key = dedup_key(row)
        if key not in source_map:
            all_rows.append(row)
            source_map[key] = "TALEND"
        else:
            source_map[key] += ";TALEND"
    print(f"✓ {len(talend_rows)} rows")
    
    # Load Python CSV
    print("Loading Python CSV...", end=" ")
    with open(PYTHON_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        python_rows = list(reader)
    duplicates_python = 0
    for row in python_rows:
        key = dedup_key(row)
        if key not in source_map:
            all_rows.append(row)
            source_map[key] = "PYTHON"
        else:
            source_map[key] += ";PYTHON"
            duplicates_python += 1
    print(f"✓ {len(python_rows)} rows ({duplicates_python} duplicates)")
    
    # Load Redshift CSV
    print("Loading Redshift CSV...", end=" ")
    with open(REDSHIFT_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        redshift_rows = list(reader)
    duplicates_redshift = 0
    for row in redshift_rows:
        key = dedup_key(row)
        if key not in source_map:
            all_rows.append(row)
            source_map[key] = "REDSHIFT"
        else:
            source_map[key] += ";REDSHIFT"
            duplicates_redshift += 1
    print(f"✓ {len(redshift_rows)} rows ({duplicates_redshift} duplicates)")

    # Load Power BI CSV
    print("Loading Power BI CSV...", end=" ")
    with open(POWERBI_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        powerbi_rows = list(reader)
    duplicates_powerbi = 0
    for row in powerbi_rows:
        key = dedup_key(row)
        if key not in source_map:
            all_rows.append(row)
            source_map[key] = "POWER_BI"
        else:
            source_map[key] += ";POWER_BI"
            duplicates_powerbi += 1
    print(f"✓ {len(powerbi_rows)} rows ({duplicates_powerbi} duplicates)")
    
    # Load Tableau CSV
    print("Loading Tableau CSV...", end=" ")
    with open(TABLEAU_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        tableau_rows = list(reader)
    duplicates_tableau = 0
    for row in tableau_rows:
        key = dedup_key(row)
        if key not in source_map:
            all_rows.append(row)
            source_map[key] = "TABLEAU"
        else:
            source_map[key] += ";TABLEAU"
            duplicates_tableau += 1
    print(f"✓ {len(tableau_rows)} rows ({duplicates_tableau} duplicates)")

    # Add SOURCE_PARSER column to all rows
    for row in all_rows:
        key = dedup_key(row)
        row['Source_Parser'] = source_map[key]
    
    # Write consolidated CSV
    print("\nWriting consolidated CSV...", end=" ")
    if all_rows:
        fieldnames = list(all_rows[0].keys())
        if 'Source_Parser' not in fieldnames:
            fieldnames.append('Source_Parser')
        
        with open(OUTPUT_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_rows)
    print(f"✓ {len(all_rows)} unique rows")
    
    # Statistics
    print("\n" + "="*75)
    print("LINEAGE CONSOLIDATION SUMMARY")
    print("="*75)
    print(f"\nInput rows:")
    print(f"  Talend:    {len(talend_rows):4d}")
    print(f"  Python:    {len(python_rows):4d}")
    print(f"  Redshift:  {len(redshift_rows):4d}")
    print(f"  Power BI:  {len(powerbi_rows):4d}")
    print(f"  Tableau:   {len(tableau_rows):4d}")
    print(f"  TOTAL:     {len(talend_rows) + len(python_rows) + len(redshift_rows) + len(powerbi_rows) + len(tableau_rows):4d}")
    
    print(f"\nDuplicates removed:")
    print(f"  Python → existing:    {duplicates_python:4d}")
    print(f"  Redshift → existing:  {duplicates_redshift:4d}")
    print(f"  Power BI → existing:  {duplicates_powerbi:4d}")
    print(f"  Tableau → existing:   {duplicates_tableau:4d}")
    print(f"  TOTAL DUPLICATES:     {duplicates_python + duplicates_redshift + duplicates_powerbi + duplicates_tableau:4d}")
    
    print(f"\nConsolidated output:")
    print(f"  Unique rows:          {len(all_rows):4d}")
    print(f"  Output file:          {OUTPUT_FILE}")
    
    # Flow breakdown
    from collections import Counter
    flows = Counter((r['Source_Platform'], r['Target_Platform']) for r in all_rows)
    print(f"\nFlow breakdown (consolidated):")
    for (src, tgt), cnt in sorted(flows.items(), key=lambda x: -x[1]):
        print(f"  {src:15s} → {tgt:15s} : {cnt:4d} rows")
    
    # Parser contribution
    contributions = Counter(source_map[dedup_key(r)] for r in all_rows)
    print(f"\nRows contributed by parser:")
    for parser, cnt in sorted(contributions.items(), key=lambda x: -x[1]):
        print(f"  {parser:15s} : {cnt:4d} rows ({100*cnt/len(all_rows):.1f}%)")
    
    # Overlaps
    overlaps = sum(1 for sources in source_map.values() if ';' in sources)
    print(f"\nRows shared across parsers:")
    print(f"  Total overlapping rows: {overlaps:4d} ({100*overlaps/len(all_rows):.1f}%)")
    
    print("="*75 + "\n")

if __name__ == "__main__":
    merge_csvs()
