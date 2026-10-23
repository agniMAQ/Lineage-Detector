#!/usr/bin/env python3
"""
Filter lineage CSV down to rows that contribute to Tableau or Power BI reports.

Given a lineage CSV (default: consolidated output), this script:
1. Builds a directed graph from source -> target per row
2. Finds report nodes (TABLEAU_REPORT, POWER_BI_REPORT)
3. Walks upstream to collect every row on paths that end at those reports
4. Writes a filtered CSV with only relevant lineage rows
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Dict, List, Set, Tuple

WORK_DIR = Path(__file__).parent
DEFAULT_INPUT = WORK_DIR / "lineage-output" / "lineage_metadata_consolidated.csv"
DEFAULT_OUTPUT = WORK_DIR / "lineage-output" / "lineage_metadata_bi_reports_only.csv"

REPORT_TYPES = {"TABLEAU_REPORT", "POWER_BI_REPORT"}


def clean(v: str) -> str:
    return (v or "").strip()


def node_key(platform: str, schema: str, obj: str, obj_type: str) -> Tuple[str, str, str]:
    return (
        clean(platform).upper(),
        clean(schema).lower(),
        clean(obj).lower(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter lineage to Tableau/Power BI report lineage only")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input lineage CSV path")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output filtered CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_csv = Path(args.input)
    output_csv = Path(args.output)

    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    with input_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    # Build edge list and reverse adjacency for upstream traversal.
    edges: List[Tuple[Tuple[str, str, str], Tuple[str, str, str]]] = []
    reverse_adj: Dict[Tuple[str, str, str], List[int]] = defaultdict(list)

    report_nodes: Set[Tuple[str, str, str]] = set()

    for i, row in enumerate(rows):
        src = node_key(
            row.get("Source_Platform", ""),
            row.get("Source_Schema", ""),
            row.get("Source_Object", ""),
            row.get("Source_Type", ""),
        )
        tgt = node_key(
            row.get("Target_Platform", ""),
            row.get("Target_Schema", ""),
            row.get("Target_Object", ""),
            row.get("Target_Type", ""),
        )

        edges.append((src, tgt))
        reverse_adj[tgt].append(i)

        if clean(row.get("Target_Type", "")).upper() in REPORT_TYPES:
            report_nodes.add(tgt)

    # Traverse upstream from every report node and collect edge indices.
    visited_nodes: Set[Tuple[str, str, str]] = set(report_nodes)
    selected_edge_indices: Set[int] = set()

    queue: deque[Tuple[str, str, str]] = deque(report_nodes)
    while queue:
        node = queue.popleft()
        for edge_idx in reverse_adj.get(node, []):
            if edge_idx in selected_edge_indices:
                continue
            selected_edge_indices.add(edge_idx)
            src_node, _ = edges[edge_idx]
            if src_node not in visited_nodes:
                visited_nodes.add(src_node)
                queue.append(src_node)

    filtered_rows = [rows[i] for i in range(len(rows)) if i in selected_edge_indices]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(filtered_rows)

    parser_counts = Counter(clean(r.get("Source_Parser", "UNKNOWN")) for r in filtered_rows)
    target_type_counts = Counter(clean(r.get("Target_Type", "")) for r in filtered_rows)

    print(f"Input rows: {len(rows)}")
    print(f"Report nodes found: {len(report_nodes)}")
    print(f"Filtered rows: {len(filtered_rows)}")
    print(f"Output: {output_csv}")
    print("\nFiltered target types:")
    for t, c in target_type_counts.most_common():
        print(f"  {t or 'UNKNOWN'}: {c}")
    print("\nFiltered rows by Source_Parser:")
    for p, c in parser_counts.most_common():
        print(f"  {p or 'UNKNOWN'}: {c}")


if __name__ == "__main__":
    main()
