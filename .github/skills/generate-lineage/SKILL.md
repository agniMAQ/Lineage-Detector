---
name: generate-lineage
description: 'Generate deterministic multi-platform data lineage from a local repository or project folder, with an optional pipeline-only LLM POC. Use for lineage extraction, lineage CSV generation, dependency mapping, or analysing Fabric, Python, SQL, Talend, Tableau, Power BI, ADF JSON, and pipeline YAML artifacts.'
argument-hint: 'Local project folder and optional output folder'
user-invocable: true
---

# Generate Lineage

Use the checked-in CLI. Do not inspect project files, choose parsers, infer dependencies, or create lineage rows in the chat layer.

## Required Input

Obtain one local repository or project folder from the user. If it was not supplied, ask for it before running the command. An output folder is optional and defaults to `<input-folder>/lineage-output`.

## Procedure

1. Invoke the CLI exactly once from this lineage package:

   ```powershell
   python tools\lineage_cli.py generate "<input-folder>"
   ```

   When the user supplied an output folder, append:

   ```powershell
   --output-dir "<output-folder>"
   ```

   Only when the user explicitly requests the LLM pipeline POC, also append:

   ```powershell
   --llm-poc
   ```

   The POC requires `LINEAGE_LLM_ENDPOINT`, `LINEAGE_LLM_API_KEY`, and `LINEAGE_LLM_MODEL` in the process environment. Never ask the user to send the API key through chat; if it is missing, tell the user to set it directly in their terminal environment.

2. Do not run platform parsers separately. Inventory, ownership, parsing, validation, deduplication, and reporting are owned by the CLI. The default command makes no LLM or SLM calls; `--llm-poc` sends only eligible unsupported pipeline JSON/YAML files to the configured endpoint.
3. Interpret CLI status and exit codes as follows:
   - `0`: `SUCCESS`
   - `1`: `PARTIAL`; valid lineage was generated, with unsupported candidates or an isolated parser failure
   - `2`: `FAILED`; no valid lineage could be published or the command input was invalid
4. Report the status and the paths to `lineage_metadata_consolidated.csv` and `lineage_execution_report.md` from the command output. For an LLM POC run, also report `lineage_metadata_llm_pipeline_poc.csv` and `lineage_metadata_consolidated_poc.csv`.
5. For `PARTIAL`, direct the user to the report diagnostics. Do not describe exit code `1` as a total failure.

## Boundaries

- Treat the input folder as read-only.
- Never fabricate unresolved objects or silently parse unsupported formats.
- Never enable `--llm-poc` unless the user explicitly requested it.
- Do not generate or modify parser code during a lineage run.
- Do not claim to query live databases, SaaS services, or Fabric workspaces.
- Preserve the canonical direction: `Source_*` is upstream and `Target_*` is downstream.
