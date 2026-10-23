#!/usr/bin/env python3
"""Audit per-subfolder notebook lineage CSV quality and consistency.

Compares parser-derived rows from notebooks under each top-level lrus_bi-master
subfolder with rows in csv/python/lineage_metadata_python_<subfolder>.csv.

Outputs:
  - csv/python/lineage_subfolder_audit_summary.csv
  - csv/python/lineage_subfolder_audit_mismatch_samples.csv
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


WORKSPACE = Path(r"c:\Repos\WK LR US Artifacts")
LRUS = WORKSPACE / "lrus_bi-master"
CSV_DIR = WORKSPACE / "csv" / "python"


def load_parser_module():
    parser_path = WORKSPACE / "silver_agent" / "agents" / "lineage-viewer" / "tools" / "parsers" / "parse_python_lineage.py"
    spec = importlib.util.spec_from_file_location("parse_python_lineage", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import parser: {parser_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def safe_name(name: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in name.lower())


def row_key_from_csv(row: dict) -> tuple:
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


def dedupe(rows: list[dict], row_key_fn) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        k = row_key_fn(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    parser = load_parser_module()
    parse_file = parser.parse_file
    row_key = parser.row_key

    subfolders = sorted([p for p in LRUS.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.name.lower())

    summary_rows = []
    mismatch_samples = []

    all_folder_csv_rows = []
    for sub in subfolders:
        notebooks = sorted(sub.rglob("*.ipynb"), key=lambda p: str(p).lower())

        expected_rows = []
        for nb in notebooks:
            try:
                expected_rows.extend(parse_file(nb, nb.parent))
            except Exception:
                continue
        expected_rows = dedupe(expected_rows, row_key)
        expected_map = {row_key(r): r for r in expected_rows}

        csv_file = CSV_DIR / f"lineage_metadata_python_{safe_name(sub.name)}.csv"
        csv_rows = load_csv_rows(csv_file)
        csv_rows = dedupe(csv_rows, row_key_from_csv)
        csv_map = {row_key_from_csv(r): r for r in csv_rows}

        missing_keys = sorted(set(expected_map) - set(csv_map))
        extra_keys = sorted(set(csv_map) - set(expected_map))

        # Quality stats for this folder CSV.
        self_edges = 0
        file_to_file = 0
        file_to_table = 0
        table_to_file = 0
        ambiguous = 0
        for r in csv_rows:
            src_s = (r.get("Source_Schema") or "").lower()
            src_o = (r.get("Source_Object") or "").lower()
            src_t = (r.get("Source_Type") or "").upper()
            tgt_s = (r.get("Target_Schema") or "").lower()
            tgt_o = (r.get("Target_Object") or "").lower()
            tgt_t = (r.get("Target_Type") or "").upper()

            if src_s == tgt_s and src_o == tgt_o and src_t == tgt_t:
                self_edges += 1
            if src_t == "FILE" and tgt_t == "FILE":
                file_to_file += 1
            if src_t == "FILE" and tgt_t == "TABLE":
                file_to_table += 1
            if src_t == "TABLE" and tgt_t == "FILE":
                table_to_file += 1
            if (r.get("Is_Ambiguous") or "").upper() == "YES":
                ambiguous += 1

        summary_rows.append({
            "Subfolder": sub.name,
            "Notebook_Count": len(notebooks),
            "Expected_Rows": len(expected_rows),
            "CSV_Rows": len(csv_rows),
            "Missing_In_CSV": len(missing_keys),
            "Extra_In_CSV": len(extra_keys),
            "Match": "YES" if not missing_keys and not extra_keys else "NO",
            "Self_Edges": self_edges,
            "File_To_File": file_to_file,
            "File_To_Table": file_to_table,
            "Table_To_File": table_to_file,
            "Ambiguous_Rows": ambiguous,
            "CSV_File": str(csv_file).replace("\\", "/"),
        })

        for k in missing_keys[:5]:
            r = expected_map[k]
            mismatch_samples.append({
                "Subfolder": sub.name,
                "Type": "MISSING_IN_CSV",
                "Source": f"{r['Source_Schema']}.{r['Source_Object']}",
                "Target": f"{r['Target_Schema']}.{r['Target_Object']}",
                "Transform_Path": r["Transform_Path"],
            })
        for k in extra_keys[:5]:
            r = csv_map[k]
            mismatch_samples.append({
                "Subfolder": sub.name,
                "Type": "EXTRA_IN_CSV",
                "Source": f"{r['Source_Schema']}.{r['Source_Object']}",
                "Target": f"{r['Target_Schema']}.{r['Target_Object']}",
                "Transform_Path": r.get("Transform_Path", ""),
            })

        all_folder_csv_rows.extend(csv_rows)

    # Master consistency: union of folder CSVs vs master CSV.
    union_rows = dedupe(all_folder_csv_rows, row_key_from_csv)
    union_keys = {row_key_from_csv(r) for r in union_rows}

    master_file = CSV_DIR / "lineage_metadata_python_master.csv"
    master_rows = dedupe(load_csv_rows(master_file), row_key_from_csv)
    master_keys = {row_key_from_csv(r) for r in master_rows}

    master_missing = sorted(union_keys - master_keys)
    master_extra = sorted(master_keys - union_keys)

    summary_rows.append({
        "Subfolder": "__MASTER_CHECK__",
        "Notebook_Count": "",
        "Expected_Rows": len(union_rows),
        "CSV_Rows": len(master_rows),
        "Missing_In_CSV": len(master_missing),
        "Extra_In_CSV": len(master_extra),
        "Match": "YES" if not master_missing and not master_extra else "NO",
        "Self_Edges": "",
        "File_To_File": "",
        "File_To_Table": "",
        "Table_To_File": "",
        "Ambiguous_Rows": "",
        "CSV_File": str(master_file).replace("\\", "/"),
    })

    for k in master_missing[:15]:
        r = next(rr for rr in union_rows if row_key_from_csv(rr) == k)
        mismatch_samples.append({
            "Subfolder": "__MASTER_CHECK__",
            "Type": "MISSING_IN_MASTER",
            "Source": f"{r['Source_Schema']}.{r['Source_Object']}",
            "Target": f"{r['Target_Schema']}.{r['Target_Object']}",
            "Transform_Path": r.get("Transform_Path", ""),
        })
    for k in master_extra[:15]:
        r = next(rr for rr in master_rows if row_key_from_csv(rr) == k)
        mismatch_samples.append({
            "Subfolder": "__MASTER_CHECK__",
            "Type": "EXTRA_IN_MASTER",
            "Source": f"{r['Source_Schema']}.{r['Source_Object']}",
            "Target": f"{r['Target_Schema']}.{r['Target_Object']}",
            "Transform_Path": r.get("Transform_Path", ""),
        })

    summary_file = CSV_DIR / "lineage_subfolder_audit_summary.csv"
    mismatch_file = CSV_DIR / "lineage_subfolder_audit_mismatch_samples.csv"

    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    mismatch_header = ["Subfolder", "Type", "Source", "Target", "Transform_Path"]
    with open(mismatch_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=mismatch_header)
        writer.writeheader()
        writer.writerows(mismatch_samples)

    mismatch_count = sum(1 for r in summary_rows if r["Subfolder"] != "__MASTER_CHECK__" and r["Match"] == "NO")
    print(f"Subfolders checked: {len(subfolders)}")
    print(f"Subfolder mismatches: {mismatch_count}")
    print(f"Master consistency match: {summary_rows[-1]['Match']}")
    print(f"Summary: {summary_file}")
    print(f"Mismatch samples: {mismatch_file}")


if __name__ == "__main__":
    main()
