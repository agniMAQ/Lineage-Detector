# Multi-Platform Lineage Decisions

## Status

This document records the agreed design for extending the current Fabric repository lineage tool into a deterministic multi-platform lineage tool.

The Fabric parser, shared skill, deterministic dispatcher, parser registry, adapters, validation pipeline, outputs, and execution report described below are implemented.

## Agreed Decisions

| Area | Decision | Purpose |
|---|---|---|
| User interface | Add `.github/skills/generate-lineage/SKILL.md`. | Give the user one workflow that accepts a local project folder. |
| Code entry point | Add `tools/lineage_cli.py`. | Keep inventory, routing, execution, and output decisions deterministic and testable. |
| Input | Require one local repository/project folder; configuration remains optional. | Support cloned repositories without requiring live-system access. |
| Release scope | Integrate all existing parsers: Fabric, Python, SQL/Redshift, Talend, Tableau, and Power BI. | Reuse the parsing logic already present under `tools/parsers`. |
| LLM/SLM policy | Make no LLM or SLM calls during inventory, parser selection, parsing, validation, or output generation. | Ensure repeatability, privacy, and predictable cost. |
| Inventory | Build inventory in Python, not in `SKILL.md`. | Ensure the same files and parsers are selected for the same repository every time. |
| Parser selection | Use a static parser registry containing detection rules, ownership, and capabilities. | Avoid conversational or filename-only parser selection. |
| Overlap policy | The most-specific parser owns a file. | Prevent duplicate lineage when one file matches multiple parsers. |
| Unknown formats | Warn and continue when a likely lineage artifact has no approved parser. | Preserve supported results while making coverage gaps visible. |
| Failure policy | Continue independent parsers after one parser fails and mark the run `PARTIAL`. | Retain valid lineage without representing the run as complete. |
| Direction | Canonical rows use `Source_*` for upstream and `Target_*` for downstream. | Match the active Fabric parser and viewer behaviour. |
| CSV contract | Preserve the existing 18-column CSV projection in the first release. | Maintain compatibility with the current Lineage Viewer. |
| Outputs | Write per-parser CSVs, one consolidated CSV, and a Markdown execution report. | Support troubleshooting, consumption, and run auditing. |
| Execution report | Use `lineage_execution_report.md`, not JSON. | Provide a readable summary of inventory, parser outcomes, warnings, failures, and row counts. |
| Repository safety | Treat the input folder as read-only and write only under its configured output directory. | Prevent accidental modification of project artifacts. |
| Parser generation | Do not generate missing parsers in this release. | Keep unapproved code out of production lineage runs. |

## Components and Their Purpose

| Component | Status | Purpose |
|---|---|---|
| `.github/skills/generate-lineage/SKILL.md` | Implemented | Collect the folder path, invoke the CLI once, and present its result. |
| `tools/lineage_cli.py` | Implemented | Primary deterministic command-line entry point. |
| `tools/inventory.py` | Implemented | Walk the input folder, exclude noise, inspect signatures, and identify lineage candidates. |
| `tools/parser_registry.py` | Implemented | Register parser IDs, detection rules, precedence, and adapter callables. |
| `tools/parser_adapters.py` | Implemented | Present all approved existing parsers through one in-memory adapter contract. |
| `tools/lineage_model.py` | Implemented | Define and normalize the canonical 18-column lineage row. |
| `tools/validation.py` | Implemented | Validate required fields, direction, enum values, evidence paths, and row shape. |
| `tools/deduplication.py` | Implemented | Remove exact duplicates while preserving distinct transforms and evidence. |
| `tools/reporting.py` | Implemented | Write `lineage_execution_report.md`. |
| `tools/parsers/parse_fabric_lineage.py` | Existing and active | Parse Fabric notebooks, DataPipeline definitions, shortcuts, TMDL semantic models, PBIR reports, and supported ingestion configuration. |
| `tools/parsers/parse_python_lineage.py` | Integrated | Parse assigned Python scripts and Jupyter notebooks using repository-relative evidence paths. |
| `tools/parsers/parse_redshift_lineage.py` | Integrated | Parse assigned SQL transformations and supported Redshift view inventory rows. |
| `tools/parsers/parse_talend_csv_lineage.py` | Integrated | Parse assigned Talend `.item` exports. |
| `tools/parsers/parse_talend_inventory_lineage.py` | Integrated | Parse assigned Talend inventory and optional call-analysis inputs. |
| `tools/parsers/parse_tableau_lineage_strict.py` | Integrated | Parse assigned Tableau metadata CSV inputs in memory. |
| `tools/parsers/parse_powerbi_lineage.py` | Integrated normalizer | Normalize supported pre-generated Power BI lineage CSVs; PBIP/TMDL/PBIR structures are handled by Fabric discovery. |

## Parser Ownership Rules

1. Fabric owns exported `notebook-content.py` files under discovered Fabric notebook folders. Generic Python must skip those files.
2. Fabric/Power BI owns `.DataPipeline`, `.SemanticModel`, TMDL, `.Report`, PBIR, and PBIP structures. Generic text or JSON handling must skip them.
3. Talend owns recognized `.item` XML and supported Talend inventory files.
4. Tableau owns files that match its required Tableau metadata signature.
5. SQL owns standalone SQL/DDL files. SQL embedded in Python or Talend remains owned by that containing artifact's parser.
6. Generic Python owns remaining supported `.py` and `.ipynb` files.
7. A file is parsed once unless a future registry rule explicitly permits shared ownership.

## Unsupported Candidate Policy

The inventory must not warn about every unrelated file. It should report only files that resemble data pipelines, transformations, database definitions, or BI metadata but do not match an approved parser.

Example report entry:

```text
Unsupported lineage candidate
- File: pipelines/customer_load.json
- Detected signature: pipeline-like JSON
- Reason: No approved parser found
```

Unsupported candidates do not trigger an LLM call and do not stop parsers that have valid inputs.

## Validation and Deduplication Decisions

1. Validate parser output before merging it.
2. Reject rows that do not have exactly the required 18 fields.
3. Require non-empty source and target objects.
4. Require the source artifact path as transform evidence when available.
5. Mark unresolved dynamic references as ambiguous; do not invent object names.
6. Deduplicate using endpoint identity, dependency type, transform tool, transform name, transform path, and column evidence.
7. Do not collapse two identical endpoints produced by different transformations.
8. Record rejected-row counts and representative reasons in the execution report.

## Output Contract

The default output directory is `<input-folder>/lineage-output/`.

```text
lineage-output/
├── lineage_metadata_fabric.csv
├── lineage_metadata_python.csv
├── lineage_metadata_sql.csv
├── lineage_metadata_talend.csv
├── lineage_metadata_tableau.csv
├── lineage_metadata_powerbi.csv
├── lineage_metadata_consolidated.csv
└── lineage_execution_report.md
```

Only parsers selected for the run need to produce a platform CSV. The consolidated CSV contains all valid, deduplicated rows from successful parsers.

## Run Status

| Status | Meaning |
|---|---|
| `SUCCESS` | All selected parsers and validation stages completed. |
| `PARTIAL` | At least one parser failed or unsupported candidates were found, but valid lineage was produced. |
| `FAILED` | No parser completed successfully, no supported artifacts were found, or consolidated output could not be validated. |

## Not Included in This Release

- Automatic parser generation.
- Automatic LLM or SLM fallback.
- Live connections to databases or SaaS platforms.
- Runtime lineage from query logs or job execution logs.
- Guaranteed parsing of binary or encrypted project formats.
- A breaking replacement for the current 18-column viewer contract.

## Quiz Decisions Recorded

- Release scope: **All existing parsers**.
- Unknown formats: **Warn and continue**.
- LLM policy: **No LLM calls**.
- Output: **Per-parser CSVs, consolidated CSV, and Markdown execution report**.
- Parser overlap: **Most-specific parser wins**.
- Parser failure: **Continue with partial status**.