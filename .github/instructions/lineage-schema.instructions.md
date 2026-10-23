---
applyTo: ".github/agents/*.agent.md"
---

# Lineage Schema Reference

Every CSV row you produce **must** conform to this 18-column schema. Use a **comma** (`,`) as the delimiter. Always include the header on the first line of a new file; append without header when adding to an existing file.

## Column Definitions

| # | Header | Required | Allowed Values / Notes |
|---|--------|----------|------------------------|
| 1 | `Source_Schema` | Yes | Schema/namespace of the **upstream provider** (e.g. `PUBLIC`, `TABLEAU_MODEL`, `SALESFORCE`). Use `NULL` if unknown. |
| 2 | `Source_Object` | Yes | Name of the upstream object being read. |
| 3 | `Source_Type` | Yes | See **Object Types** below. |
| 4 | `Target_Schema` | Yes | Schema/namespace of the **downstream consumer**. |
| 5 | `Target_Object` | Yes | Name of the downstream object being produced or consuming the source. |
| 6 | `Target_Type` | Yes | See **Object Types** below. |
| 7 | `Dependency_Type` | Yes | `DATA_FLOW` \| `ORCHESTRATION` \| `SHORTCUT` |
| 8 | `Column_Or_Parameter` | Yes | Target column name for column-level rows; `*` for table-level rows. |
| 9 | `Schema_Bound` | Yes | `Yes` \| `No` |
| 10 | `External_Location` | Yes | Connection string or server name for external systems; `NULL` for local. |
| 11 | `Is_Ambiguous` | Yes | `Yes` if the mapping is uncertain (dynamic SQL, f-strings, complex expressions); `No` otherwise. |
| 12 | `Location_Type` | Yes | `LOCAL` \| `EXTERNAL` \| `REMOTE` |
| 13 | `Source_Platform` | Yes | See **Platforms** below. |
| 14 | `Target_Platform` | Yes | See **Platforms** below. |
| 15 | `Transform_Tool` | Yes | Parser-emitted tool token such as `TALEND`, `PYTHON`, `SQL`, `FABRIC_PIPELINE`, `POWER_BI_MODEL`, `POWER_BI_REPORT`, `SHORTCUT`, or `None`. |
| 16 | `Transform_Name` | Yes | Job name, script filename, view name, or stored procedure name. `NULL` if not applicable. |
| 17 | `Transform_Path` | Yes | Relative file path to the transform definition (e.g. `jobs/Sales/JOB_SALES_LOAD.item`). `NULL` if not applicable. |
| 18 | `Source_Column` | Yes | Source column for column-level rows; `NULL` for table-level rows. |

---

## Allowed Object Types (col 3, col 6)

```
TABLE              USER_TABLE           VIEW
TALEND_JOB         PYTHON_SCRIPT        TABLEAU_REPORT
POWER_BI_REPORT    SOURCE_TABLE         S3_FILE
API_ENDPOINT       SQL_STORED_PROCEDURE SQL_SCALAR_FUNCTION
SQL_INLINE_TABLE_VALUED_FUNCTION  SQL_TRIGGER  CHECK_CONSTRAINT
FILE
PIPELINE            NOTEBOOK             <ACTIVITY_TYPE>_ACTIVITY
```

When type cannot be determined, use `TABLE` as the fallback.

---

## Allowed Platforms (col 13, col 14)

```
REDSHIFT    SALESFORCE    SAP         S3          AZURE
TABLEAU     POWER_BI      GAINSIGHT   PYTHON      TALEND
ORACLE      MYSQL         FILE        API         EC2
FABRIC_BRONZE  FABRIC_SILVER  FABRIC_GOLD  FABRIC_LAKEHOUSE
FABRIC_PIPELINE  FABRIC_NOTEBOOK
```

When platform cannot be determined, use `REDSHIFT` as default for SQL objects or `FILE` for local files.

---

## Direction Convention — CRITICAL

```
Source (cols 1-3) = UPSTREAM PROVIDER    <- the object that PROVIDES the data
Target (cols 4-6) = DOWNSTREAM CONSUMER  <- the object that USES the data
```

Data flows: **Source -> Target** (from upstream to downstream).

Examples:
- A Tableau report reading from a Redshift view: Source=view, Target=Tableau model/report
- A Redshift view reading from a table: Source=table, Target=view
- A Talend job loading Salesforce data into Redshift: Source=Salesforce object, Target=Redshift table

---

## Example Rows (comma-delimited shown with `|` for readability)

**Redshift view -> Tableau report:**
```
PUBLIC | VW_SALES_BY_REGION | VIEW | TABLEAU_REPORTS | Sales_By_Region | TABLEAU_REPORT | DATA_FLOW | * | No | NULL | No | LOCAL | REDSHIFT | TABLEAU | None | Sales_By_Region | reports/sales.twb | NULL
```

**Redshift table -> Redshift view (table-level):**
```
PUBLIC | FACT_SALES | TABLE | PUBLIC | VW_SALES_BY_REGION | VIEW | DATA_FLOW | * | No | NULL | No | LOCAL | REDSHIFT | REDSHIFT | SQL | vw_sales_by_region | sql/views/vw_sales.sql | NULL
```

**Salesforce -> Redshift via Talend (table-level):**
```
SALESFORCE | OPPORTUNITY | SOURCE_TABLE | PUBLIC | FACT_SALES | TABLE | DATA_FLOW | * | No | salesforce.com | No | EXTERNAL | SALESFORCE | REDSHIFT | TALEND | JOB_SALES_LOAD | jobs/Sales/JOB_SALES_LOAD.item | NULL
```

---

## Output File

Write parser rows to their platform CSV and merged rows to `lineage-output/lineage_metadata_consolidated.csv`.

- If the file does **not** exist: create it with the 18-column header line first.
- Replace generated files for each run; do not append stale rows from earlier runs.
- Deduplicate using endpoint identity plus dependency type, transform tool, transform name, transform path, and column evidence.
- Use **comma** (`,`) as the delimiter.
- Wrap values containing commas or newlines in double quotes.
- Replace newlines within values with a space.

---

## Confidence and Ambiguity Rules

| Situation | Is_Ambiguous | Action |
|-----------|-------------|--------|
| Direct table reference found | No | Normal row |
| Custom SQL parsed successfully | No | Normal row |
| f-string or `.format()` SQL | Yes | Extract static parts; note in Transform_Name |
| Dynamic SQL built at runtime | Yes | Extract table names from string fragments |
| Unknown Talend component | Yes | Reason from elementParameter names |
| Tableau calculated field formula | No | Extract field refs for column-level |
| SQL in Python variable not traceable | Yes | Use `*` for columns |

---

## Anti-Hallucination Guardrails

These rules govern schema compliance for every agent that writes to `lineage_metadata.csv`.

1. **Exactly 18 columns per row** — Every emitted row must have exactly 18 comma-delimited fields. Rows with more or fewer columns will break the Lineage Viewer CSV parser. Verify the column count before appending.
2. **No invented column values** — Every field value must come from the source file being parsed. Do not fill empty fields with guessed values; use the documented defaults (`NULL`, `No`, `*`, `UNKNOWN`) instead.
3. **`Source` = upstream provider; `Target` = downstream consumer** — Data flows `Source -> Target`. Never reverse this.
4. **`Dependency_Type` must be one of the enumerated values** — Only use `DATA_FLOW`, `ORCHESTRATION`, or `SHORTCUT` in the current contract.
5. **`Is_Ambiguous` is boolean string** — Only the values `Yes` and `No` are valid. Do not use `TRUE`, `FALSE`, `1`, `0`, or blank.
6. **`Schema_Bound` is boolean string** — Only `Yes` and `No`. Default to `No` unless the SQL explicitly uses `WITH SCHEMABINDING`.
7. **`NULL` means unknown, not empty string** — Use the literal string `NULL` (no quotes in CSV) when a field cannot be determined. Do not use an empty field (two consecutive commas) unless the column is intentionally blank per the schema spec.
8. **`Source_Column` is the raw upstream column name** — This field holds the physical column name from the source table, not the alias or output field name. Do not emit the alias here.
9. **No extra columns** — The 18-column schema is fixed. Do not append additional columns for debugging, notes, or metadata. Use the `Transform_Name` field for parser annotations.
10. **Header row must appear exactly once** — The header `Source_Schema,Source_Object,...` must be the first line of the CSV. Never re-emit the header as a data row when appending.
