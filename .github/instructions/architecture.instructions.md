---
applyTo: ".github/agents/*.agent.md,.github/prompts/*.prompt.md"
---

# Current Data Architecture — Wolters Kluwer LR US

## Overview
This document describes the end-to-end data architecture that all lineage parser agents must understand. Every agent operates within this architecture when inferring platforms, schemas, transformation tools, and data flows.

---

## Architecture Layers (left → right)

### 1. Data Sources (External / Upstream)
These are the raw origin systems. When a lineage row has no further upstream dependency, its `Target_Platform` will be one of these, and `Target_Type` will be `SOURCE_TABLE` or `EXTERNAL`.

| System | Platform Token | Notes |
|---|---|---|
| Salesforce (CRM) | `SALESFORCE` | Employee, Department, Account objects via SOAP/REST API |
| SAP | `SAP` | ERP data extracts |
| GONG | `GONG` | Conversation intelligence |
| Analytics / Outreach | `OUTREACH` | Sales engagement |
| CG (Campaign Grid) | `CG` | Campaign data |
| Eloqua | `ELOQUA` | Marketing automation |
| Tagetik | `TAGETIK` | Corporate performance management |
| Conductor | `CONDUCTOR` | SEO/content analytics |
| LinkedIn Ads | `LINKEDIN` | Paid media data |
| Gainsight (source) | `GAINSIGHT` | Customer success data |
| JOPARD SOLUTIONS | `JOPARD` | Professional services data |
| ICADMIN | `ICADMIN` | Incentive compensation |
| Azure | `AZURE` | Azure-hosted data / Azure Data Lake |
| FAB Studio | `FAB_STUDIO` | Internal BI/ML platform |

---

### 2. Ingestion Layer
Raw data is moved from source systems into the warehouse using two tools:

#### Talend Open Source
- **File format**: `.item` XML (Talend Data Integration / Open Studio) OR `.json` (Talend Cloud / Data Fabric)
- **Common components**: `tSalesforceInput`, `tRedshiftInput`, `tS3Get`, `tFileInputDelimited`, `tRedshiftOutput`, `tRedshiftBulkExec`, `tMap`
- **Transform_Tool**: `TALEND`
- **Transform_Path convention**: `talend/jobs/<domain>/<JOB_NAME>/<JOB_NAME>.item`  (XML)  or `talend-json/<job_name>.json` (JSON)
- **Typical flow**: `SOURCE_SYSTEM → tMap → STAGING schema in Redshift`

#### Python (Ingestion scripts)
- **File location**: `scripts/` folder, named `load_*.py`
- **Libraries used**: `boto3` (S3), `psycopg2` (direct Redshift), `pandas` + `sqlalchemy`
- **Transform_Tool**: `PYTHON`
- **Transform_Path convention**: `scripts/<script_name>.py`
- **Typical flow**: `S3 → pandas DataFrame → STAGING schema in Redshift`

---

### 3. Staging / Intermediate Storage

#### Amazon S3
- **Role**: Intermediate file staging, raw data lake, ML artefact storage
- **Platform token**: `S3`
- **Bucket naming pattern**: `hr-data-lake/`, `wk-lr-us-<domain>/`
- **Object type in lineage**: `S3_FILE`
- **Location_Type**: `EXTERNAL`

#### Amazon Redshift — STAGING Schema
- **Platform token**: `REDSHIFT`
- **Schema**: `STAGING`
- **Purpose**: Raw / lightly cleansed data landed from source systems before transformation
- **Typical tables**: `STAGING.SF_EMPLOYEE`, `STAGING.HR_ATTRITION_RAW`, `STAGING.SF_DEPARTMENT`

---

### 4. Core Data Warehouse

#### Amazon Redshift — DWH Schema
- **Platform token**: `REDSHIFT`
- **Schema**: `DWH`
- **Purpose**: Modelled, cleansed, business-ready data
- **Object types**: `TABLE` (fact/dimension), `VIEW` (`VW_` prefix), `SQL_STORED_PROCEDURE` (`SP_` prefix)
- **Naming conventions**:
  - Facts: `DWH.FACT_<DOMAIN>`
  - Views: `DWH.VW_<DOMAIN>_<SUBJECT>`
  - Stored procs: `DWH.SP_<ACTION>_<SUBJECT>`
  - Metric snapshots: `DWH.HR_<SUBJECT>_METRICS` / `DWH.<SUBJECT>_SNAPSHOT`

---

### 5. Compute / Orchestration

#### Amazon EC2 — Production Instances
- **Platform token**: `EC2`
- **Role**: Hosts production Talend jobs, Python schedulers, and Redshift stored procedure orchestration
- **Does NOT store data**: EC2 is compute only; it reads/writes S3 and Redshift

#### Python — Scheduling & Refresh
- **Transform_Tool**: `PYTHON`
- **Role**: Orchestrates refresh cycles between EC2, S3, and Redshift
- **File location**: `scripts/` — files named `refresh_*.py`, `schedule_*.py`, `load_*.py`
- **Libraries**: `boto3`, `psycopg2`, `sqlalchemy`, `pandas`

#### FAB Studio
- **Platform token**: `FAB_STUDIO`
- **Role**: Internal BI platform; also drives ML/predictive analytics workflows via Python
- **Connects to**: Python (for ML model training), Redshift DWH (for report data)

---

### 6. Transformation Layer

#### SQL Transformations (Redshift)
- **Files**: `sql/views/*.sql`, `sql/procedures/*.sql`
- **Transform_Tool**: `SQL`
- **Patterns**: `CREATE OR REPLACE VIEW`, `CREATE TABLE AS SELECT`, `INSERT INTO ... SELECT`, `CALL sp_*`
- **CTE patterns**: Common; resolve CTEs to their base tables for lineage
- **Redshift-specific**: `DISTKEY`, `SORTKEY`, `ENCODE`, `COPY FROM S3`, `UNLOAD TO S3`

#### Python Transformations
- **Files**: `scripts/transform_*.py`
- **Transform_Tool**: `PYTHON`
- **Patterns**: `pd.read_sql`, `df.to_sql`, psycopg2 execute, computed columns added via pandas
- **Typical flow**: `DWH.VW_* → pandas → DWH.<METRICS_TABLE>`

---

### 7. Consumption Layer (Downstream / Reporting)

Data consumers are the final downstream objects. When a lineage row represents a report consuming Redshift data, `Source_Platform` is the reporting tool and `Source_Type` describes the report artifact.

| System | Platform Token | Source_Type | Notes |
|---|---|---|---|
| Tableau | `TABLEAU` | `TABLEAU_REPORT` | `.twb` / `.twbx`; connects to Redshift via JDBC |
| Power BI | `POWER_BI` | `POWER_BI_REPORT` | `.pbix`; DirectQuery or Import mode |
| Gainsight (destination) | `GAINSIGHT` | `GAINSIGHT_REPORT` | Receives aggregated metrics |
| Salesforce (destination) | `SALESFORCE` | `SF_REPORT` | Write-back or reporting objects |

**Tableau specifics**:
- Connection class: `redshift`
- Server pattern: `*.redshift.amazonaws.com`
- Calculated fields reference Redshift column names
- Worksheets → Dashboard relationships captured as `DATA_FLOW` dependencies

---

## Standard Schema / Platform Mapping

| Schema | Platform | Environment |
|---|---|---|
| `STAGING` | `REDSHIFT` | Landing zone |
| `DWH` | `REDSHIFT` | Core warehouse |
| `SALESFORCE` | `SALESFORCE` | External CRM |
| `S3` or `NULL` (with ext_loc) | `S3` | Data lake |
| `NULL` (file path) | `FILE` | Local/SFTP files |
| `EC2` | `EC2` | Compute node |

---

## Data Flow Direction (critical for lineage CSV schema)

```
DATA SOURCES → [Talend/Python ingestion] → STAGING → [SQL/Python transform] → DWH → [Tableau/Power BI/Gainsight]
     ↓                                         ↑
Amazon S3 ──────────────────────────────────────
     ↕
Amazon EC2 (scheduling/orchestration)
     ↕
Python ↔ FAB Studio ↔ ML/Predictive Analytics
```

**CSV schema direction reminder**:
- `Source_*` columns (1–3) = the **downstream consumer** (what reads the data)
- `Target_*` columns (4–6) = the **upstream dependency** (where data comes from)
- Data flows: `Target → Source`

---

## Domain Coverage

The actual domain of data flowing through this architecture is **unknown at parse time**. Agents must not assume any specific subject area (HR, Finance, Sales, etc.). Objects from any business domain may appear — infer domain context only from the file/table names encountered, never hardcode or assume it.

**When parsing, treat all of the following as valid domain indicators discovered at runtime:**
- Salesforce object names (e.g., `Account`, `Opportunity`, `Custom__c` objects)
- Redshift schema and table names (e.g., `STAGING.*`, `DWH.*`, `FINANCE.*`, `SALES.*`)
- S3 bucket/key paths
- Tableau workbook and datasource names
- Python script names and SQL view names

Do not filter, skip, or make assumptions based on domain. Parse all files discovered and emit lineage rows for whatever data objects are present.

---

## File Structure Reference

```
c:\Repos\WK LR US\
├── .github/
│   ├── agents/          # Lineage parser agents (.agent.md)
│   ├── instructions/    # Schema + architecture instructions
│   └── prompts/         # Orchestration prompts (.prompt.md)
├── talend/jobs/         # Talend XML .item files
├── talend-json/         # Talend JSON job files
├── sql/
│   ├── views/           # CREATE VIEW .sql files
│   └── procedures/      # Stored procedure .sql files
├── scripts/             # Python ETL scripts (.py)
├── tableau/             # Tableau workbooks (.twb, .twbx)
└── lineage-output/
    └── lineage_metadata.csv   # Final 18-column lineage output
```

---

## Anti-Hallucination Guardrails

These rules apply to every agent and prompt that references this architecture document.

1. **This document describes the architecture pattern, not the actual data** — The platform names, schema zones, and component types listed here are structural templates. Do not assume that every source system listed here has data in a given workspace; confirm by finding actual files.
2. **Do not invent table names from schema zone names** — Knowing that `STAGING` is a Redshift schema zone does not mean a table called `STAGING.ANY_NAME` exists. Only emit rows for tables found in actual source files.
3. **Platform tokens are enumerated** — The only valid `Source_Platform` and `Target_Platform` values are those listed in the Platform→Schema Mapping section of this document, plus `UNKNOWN` and `FILE`. Do not invent new platform tokens.
4. **Schema zones are not platforms** — `STAGING`, `DWH`, and `RAW` are Redshift schema names, not platform tokens. The platform for all Redshift-resident objects is `REDSHIFT`.
5. **Domain is unknown at parse time** — This architecture is domain-agnostic. Do not assume table names, column names, or business entities from the architecture description. Extract them only from actual file content.
6. **Tool chain does not imply data flow** — The fact that Talend and Python are both listed as ETL tools does not mean every Talend output is also consumed by a Python script. Only emit relationships backed by explicit file evidence.
7. **S3 paths must come from files** — Do not construct or assume S3 bucket/key paths from architecture knowledge. Only use paths found in actual source file content.
8. **EC2/FAB Studio are compute locations, not data sources** — These are execution environments. Do not emit lineage rows that list EC2 or FAB Studio as a source or target object.
