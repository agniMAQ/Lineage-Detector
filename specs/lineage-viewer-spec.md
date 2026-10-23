# Lineage Viewer — Spec

## Requirement

`silver-pipeline`'s dependency graph builder needs a single, authoritative, table-to-table
lineage edge list covering every source system in the LRUS_BI estate: Talend jobs (CSV
schedule export + JSON job inventory), Python notebooks/scripts, Redshift SQL views, Tableau
workbooks, and Power BI reports. No single source system's native metadata covers all edges by
itself, so this agent parses each source independently and merges the results into one
consolidated, deduplicated edge list — plus a standalone browser viewer so a human can inspect
any exported CSV without extra tooling.

## Design

### Per-source parsers (`tools/parsers/`)
Each source gets its own parser script that reads that system's native export/metadata and
emits a lineage CSV in the shared 18-column schema (raw Power BI export is 19 columns before
`parse_powerbi_lineage.py`'s in-place cleanup drops the extra column):

| Source | Script | Output |
|---|---|---|
| Talend (CSV schedule) | `parse_talend_csv_lineage.py` | `lineage_metadata_talend_csv.csv` |
| Talend (JSON job inventory) | `parse_talend_inventory_lineage.py` | `lineage_metadata_talend_inventory.csv` |
| Python | `parse_python_lineage.py` | `lineage_metadata_python.csv` |
| Redshift SQL | `parse_redshift_lineage.py` | `lineage_metadata_redshift.csv` |
| Tableau | `parse_tableau_lineage_strict.py` | `lineage_metadata_tableau.csv` (+ `tableau_missing_source_tables.csv`) |
| Power BI | `parse_powerbi_lineage.py` | `lineage_metadata_powerbi.csv` (in-place cleanup) |

Schema and per-platform token conventions are pinned in
`.github/instructions/lineage-schema.instructions.md`; do not add columns or platform tokens
without updating that file first.

### Merge (`tools/merge/merge_lineage_csvs.py`)
Reads every per-source CSV above from `silver_agent/build/lineage-viewer/lineage-output/` and
writes `lineage_metadata_consolidated.csv` — the single file `silver-pipeline`'s
`dependency_graph_builder.py` consumes via `lineage_consolidated_csv_path()` in
`shared/skills/common-tools/_common.py`.

### Analysis (`tools/analysis/`)
Ad hoc, non-authoritative audit scripts that cross-check parser output against raw source
inventories (e.g. `analyze_talend_subfolder_coverage.py` checks every Talend subfolder is
represented in the CSV). These never feed back into the consolidated file automatically —
findings are manually triaged and used to fix a parser if it's under-covering a source.

### Viewer (`Lineage Viewer.html`)
Self-contained HTML/JS app with a file picker; loads any lineage CSV (typically the
consolidated export) and renders it as an interactive graph/table. No server, no build step,
no dependency on the Python tools at runtime.

## Acceptance Criteria

1. Every row in every per-source CSV and the consolidated CSV has exactly the schema's column
   count — no ragged rows.
2. `merge_lineage_csvs.py` runs clean against a fresh set of per-source CSVs and produces a
   consolidated file with no duplicate `(source_table, target_table, platform)` edges.
3. `silver-pipeline`'s `dependency_graph_builder.py` can locate and parse the consolidated CSV
   via `lineage_consolidated_csv_path()` without any path changes on its side.
4. `Lineage Viewer.html` opens the consolidated CSV without errors and renders all edges.
5. Regenerating any single source's CSV and re-running the merge step does not require
   touching any other source's parser.

## Out of scope

- Fabric Data Pipeline / notebook generation (owned by `silver-pipeline` / `silver`).
- Resolving the unrelated `c:\Repos\WK LR US Artifacts` hardcoded path present in some legacy
  scripts — pre-existing, tracked separately, not part of this agent's contract.
