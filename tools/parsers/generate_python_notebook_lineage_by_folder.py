#!/usr/bin/env python3
"""
Generate Python notebook lineage CSVs by top-level folder.

Outputs:
  - csv/python/lineage_metadata_python_<subfolder>.csv
  - csv/python/lineage_metadata_python_master.csv

Source scope:
  - lrus_bi-master/<subfolder>/**/*.ipynb
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


WORKSPACE = Path(r"c:\Repos\Scheduler")
LRUS_BI_MASTER = WORKSPACE / "lrus_bi-master"
OUTPUT_DIR = WORKSPACE / "csv" / "python"


def load_python_parser_module():
    parser_path = WORKSPACE / "silver_agent" / "agents" / "lineage-viewer" / "tools" / "parsers" / "parse_python_lineage.py"
    spec = importlib.util.spec_from_file_location("parse_python_lineage", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load parser module from {parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_name(name: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in name.lower())


def dedupe_rows(rows: list[dict], row_key_fn) -> list[dict]:
    seen = set()
    unique = []
    for row in rows:
        k = row_key_fn(row)
        if k in seen:
            continue
        seen.add(k)
        unique.append(row)
    return unique


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if not LRUS_BI_MASTER.exists():
        raise FileNotFoundError(f"Folder not found: {LRUS_BI_MASTER}")

    parser = load_python_parser_module()
    parse_file = parser.parse_file
    row_key = parser.row_key
    header = parser.CSV_HEADER

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    subfolders = sorted(
        [p for p in LRUS_BI_MASTER.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda p: p.name.lower(),
    )

    all_master_rows = []
    print(f"Top-level subfolders found: {len(subfolders)}")

    for sub in subfolders:
        notebooks = sorted(sub.rglob("*.ipynb"), key=lambda p: str(p).lower())
        folder_rows = []

        for nb in notebooks:
            try:
                folder_rows.extend(parse_file(nb, nb.parent))
            except Exception as exc:
                print(f"  [WARN] {nb}: {exc}")

        folder_rows = dedupe_rows(folder_rows, row_key)
        all_master_rows.extend(folder_rows)

        out_file = OUTPUT_DIR / f"lineage_metadata_python_{safe_name(sub.name)}.csv"
        write_csv(out_file, header, folder_rows)
        print(f"  {sub.name}: notebooks={len(notebooks)} rows={len(folder_rows)} -> {out_file.name}")

    master_rows = dedupe_rows(all_master_rows, row_key)
    master_file = OUTPUT_DIR / "lineage_metadata_python_master.csv"
    write_csv(master_file, header, master_rows)

    print()
    print("Done")
    print(f"Subfolder CSVs written to: {OUTPUT_DIR}")
    print(f"Master CSV: {master_file}")
    print(f"Master rows (deduped): {len(master_rows)}")


if __name__ == "__main__":
    main()
