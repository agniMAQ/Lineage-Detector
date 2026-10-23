# .specify — Spec-Driven Development config (lineage-viewer agent)

This folder holds the Spec-Driven Development (SDD) configuration for the `lineage-viewer`
agent: process notes that govern how the spec in [`../specs/`](../specs) is written, reviewed,
and kept in sync with the implementation in [`../tools/`](../tools).

## Process

1. **Spec first.** Any new lineage source, schema change, or merge-rule change starts as an
   update to `../specs/lineage-viewer-spec.md` before code is written or changed.
2. **Spec shape.** The spec covers: Requirement (what/why), Design (per-source parser
   contract + the 18-column consolidated schema), Acceptance Criteria (how we know it's
   done), and the concrete `tools/` script(s) that implement it.
3. **Traceability.** Every parser subagent under `../.github/agents/*.agent.md` must reference
   the exact script path under `../tools/parsers/` (or `../tools/merge/`) it operates on, so a
   reviewer can go from source system -> subagent -> script in one hop.
4. **No silent scope creep.** If a source system needs a new column or platform token beyond
   the documented 18/19-column schema, stop and update `../.github/instructions/lineage-schema.instructions.md`
   and the spec first — do not expand the schema directly in code.

## Constitution

Hard rules are the 18-column consolidated schema and per-platform token conventions defined in
`../.github/instructions/lineage-schema.instructions.md` and `../.github/instructions/architecture.instructions.md`.
Specs and subagents must not contradict them.
