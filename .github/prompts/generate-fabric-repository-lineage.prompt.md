---
description: "Generate the existing Fabric lineage CSV outputs from a locally cloned Azure DevOps or Git repository."
name: "Generate Fabric Repository Lineage"
agent: "Fabric Repository Lineage"
argument-hint: "<local repository folder path> [optional lineage.config.json path]"
---

Generate Fabric lineage for the local repository folder supplied with this prompt. Follow the selected agent's repository validation, parser execution, and reporting workflow. Do not edit source artifacts or infer lineage outside the deterministic parser output.