#!/usr/bin/env python3
"""Audit Talend lineage coverage vs generated Talend CSV."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


WORKSPACE = Path(r"c:\Repos\WK LR US Artifacts")
PROCESS_DIR = WORKSPACE / "LRUS_BI" / "process"
TALEND_CSV = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "lineage_metadata_talend_csv.csv"
OUT_SUMMARY = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "talend_lineage_audit_summary.csv"
OUT_MISSING = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "talend_lineage_missing_items.csv"


def main() -> None:
    items = sorted([p for p in PROCESS_DIR.rglob("*.item") if p.is_file()], key=lambda p: str(p).lower())

    with open(TALEND_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    transform_paths = {
        (r.get("Transform_Path") or "").replace("\\", "/").lower()
        for r in rows
        if (r.get("Transform_Path") or "").strip()
    }

    covered_items = []
    missing_items = []
    for item in items:
        rel = str(item.relative_to(WORKSPACE)).replace("\\", "/").lower()
        if rel in transform_paths:
            covered_items.append(item)
        else:
            missing_items.append(item)

    file_total = Counter()
    file_covered = Counter()
    row_total = Counter()
    row_amb = Counter()

    for item in items:
        rel = item.relative_to(PROCESS_DIR)
        top = rel.parts[0].lower() if len(rel.parts) > 1 else "__root__"
        file_total[top] += 1

    for item in covered_items:
        rel = item.relative_to(PROCESS_DIR)
        top = rel.parts[0].lower() if len(rel.parts) > 1 else "__root__"
        file_covered[top] += 1

    for r in rows:
        tp = (r.get("Transform_Path") or "").replace("\\", "/").lower()
        if not tp.startswith("lrus_bi/process/"):
            continue
        rest = tp[len("lrus_bi/process/") :]
        top = rest.split("/", 1)[0] if "/" in rest else "__root__"
        row_total[top] += 1
        if (r.get("Is_Ambiguous") or "").upper() == "YES":
            row_amb[top] += 1

    summary_rows = []
    for top in sorted(file_total.keys()):
        summary_rows.append(
            {
                "Subfolder": top,
                "Item_Files_Total": file_total[top],
                "Item_Files_Covered": file_covered[top],
                "Coverage_Pct": round((file_covered[top] / file_total[top] * 100.0), 2) if file_total[top] else 0.0,
                "Lineage_Rows": row_total[top],
                "Ambiguous_Rows": row_amb[top],
            }
        )

    totals = {
        "Subfolder": "__TOTAL__",
        "Item_Files_Total": len(items),
        "Item_Files_Covered": len(covered_items),
        "Coverage_Pct": round((len(covered_items) / len(items) * 100.0), 2) if items else 0.0,
        "Lineage_Rows": len(rows),
        "Ambiguous_Rows": sum(1 for r in rows if (r.get("Is_Ambiguous") or "").upper() == "YES"),
    }
    summary_rows.append(totals)

    with open(OUT_SUMMARY, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Subfolder",
                "Item_Files_Total",
                "Item_Files_Covered",
                "Coverage_Pct",
                "Lineage_Rows",
                "Ambiguous_Rows",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    with open(OUT_MISSING, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Missing_Item_Path"])
        writer.writeheader()
        for p in missing_items:
            writer.writerow({"Missing_Item_Path": str(p.relative_to(WORKSPACE)).replace("\\", "/")})

    print(f"Talend item files: {len(items)}")
    print(f"Talend covered items: {len(covered_items)}")
    print(f"Talend missing items: {len(missing_items)}")
    print(f"Talend lineage rows: {len(rows)}")
    print(f"Talend ambiguous rows: {totals['Ambiguous_Rows']}")
    print(f"Summary: {OUT_SUMMARY}")
    print(f"Missing list: {OUT_MISSING}")


if __name__ == "__main__":
    main()
