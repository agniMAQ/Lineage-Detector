#!/usr/bin/env python3
"""
parse_talend_csv_lineage.py
───────────────────────────
Parses all Talend .item files in the LRUS_BI/process directory and emits
lineage CSV rows for all source and target components.

Output: silver_agent/build/lineage-viewer/lineage-output/lineage_metadata_talend_csv.csv

Schema (18 columns, matching lineage-schema.instructions.md):
  Source_Schema, Source_Object, Source_Type, Target_Schema, Target_Object,
  Target_Type, Dependency_Type, Column_Or_Parameter, Schema_Bound,
  External_Location, Is_Ambiguous, Location_Type, Source_Platform,
  Target_Platform, Transform_Tool, Transform_Name, Transform_Path,
  Source_Column
"""

import csv
import os
import re
import sys
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

# ─────────────────────────── paths ───────────────────────────
WORKSPACE        = Path(r"c:\Repos\Scheduler")
LRUS_BI_PROCESS  = WORKSPACE / "LRUS_BI" / "process"
SCHEDULE_CSV     = WORKSPACE / "csv" / "Talend Schedule and Path(Sheet2).csv"
OUTPUT_DIR       = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output"
OUTPUT_FILE      = OUTPUT_DIR / "lineage_metadata_talend_csv.csv"

CSV_HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column",
]

# ─────────────── component classification maps ───────────────
SOURCE_COMPONENTS = {
    "tSalesforceInput":          "SALESFORCE",
    "tSalesforceRecurrenceInput": "SALESFORCE",
    "tRedshiftInput":            "REDSHIFT",
    "tDBInput":                  "REDSHIFT",
    "tJDBCInput":                "REDSHIFT",
    "tOracleInput":              "ORACLE",
    "tMysqlInput":               "MYSQL",
    "tS3Get":                    "S3",
    "tAzureBlobInput":           "AZURE",
    "tFileInputDelimited":       "FILE",
    "tFileInputJSON":            "FILE",
    "tFileInputExcel":           "FILE",
    "tInput":                    "REDSHIFT",
    "tGainsightInput":           "GAINSIGHT",
    "tFTPGet":                   "SFTP",
    "tHTTPInput":                "API",
    "tRESTClient":               "API",
}

TARGET_COMPONENTS = {
    "tRedshiftOutput":           "REDSHIFT",
    "tRedshiftBulkExec":         "REDSHIFT",
    "tDBOutput":                 "REDSHIFT",
    "tJDBCOutput":               "REDSHIFT",
    "tOracleOutput":             "ORACLE",
    "tMysqlOutput":              "MYSQL",
    "tS3Put":                    "S3",
    "tAzureBlobOutput":          "AZURE",
    "tFileOutputDelimited":      "FILE",
    "tFileOutputJSON":           "FILE",
    "tSalesforceOutput":         "SALESFORCE",
    "tGainsightOutput":          "GAINSIGHT",
    "tFTPPut":                   "FILE",
}

TRANSFORM_COMPONENTS = {
    "tMap", "tAggregateRow", "tFilterRow", "tDenormalize",
    "tNormalize", "tSortRow", "tJoin", "tUniqRow",
    "tDBConnection", "tRedshiftConnection", "tJDBCConnection",
    "tPrejob", "tPostjob", "tRunJob", "tLogCatcher",
    "tStatCatcher", "tWarn", "tDie", "tJava", "tJavaRow",
}

# Parameter names → object identity
SOURCE_TABLE_PARAMS = ["TABLENAME", "TABLE_NAME", "TABLE", "MODULENAME", "OBJECT_NAME"]
SOURCE_SCHEMA_PARAMS = ["SCHEMANAME", "SCHEMA_NAME", "SCHEMA_DB", "SCHEMA"]
TARGET_TABLE_PARAMS  = ["TABLENAME", "TABLE_NAME", "TABLE", "MODULENAME", "OBJECT_NAME"]
TARGET_SCHEMA_PARAMS = ["SCHEMANAME", "SCHEMA_NAME", "SCHEMA_DB", "SCHEMA"]
FILE_PARAMS          = ["FILENAME", "BUCKET", "KEY", "OUTPUT_FILE"]
QUERY_PARAMS         = ["QUERY", "SQL"]

# ─────────────── helpers ─────────────────────────────────────

def strip_version(stem: str) -> str:
    """Strip trailing _<major>.<minor> version suffix from a Talend job stem.
    e.g. 'Batch_ica_LRUSLake_All_0.1' -> 'Batch_ica_LRUSLake_All'
    """
    return re.sub(r'_\d+\.\d+$', '', stem)


def strip_quotes(v: str) -> str:
    """Remove surrounding Talend XML quote-escapes and whitespace."""
    if not v:
        return ""
    v = v.strip()
    # Handle XML-escaped quotes: &quot; → "
    v = v.replace("&quot;", '"')
    # Strip surrounding double-quotes
    if v.startswith('"') and v.endswith('"') and len(v) > 1:
        v = v[1:-1]
    return v.strip()


def simplify_filename(raw: str) -> str:
    """
    Given a potentially complex Talend FILENAME expression (possibly containing
    string concatenations like context.path + "myfile.csv"), extract the most
    meaningful filename segment.

    Strategy:
    1. If there are quoted literals, return the last non-path-separator one.
    2. If the expression contains ' + ' splits, take the last token.
    3. Otherwise return as-is.

    Examples:
      'context.path + "myfile.csv"'               → 'myfile.csv'
      '"C:/data/" + context.sub + "f.dat"'        → 'f.dat'
      'C:/data/ + SalesForce/ + myfile.json'      → 'myfile.json'
      'context.DownloadFileName'                  → 'context.DownloadFileName'
      '"simple.csv"'                              → 'simple.csv'
    """
    if not raw:
        return raw
    # Find all quoted string literals in the expression
    literals = re.findall(r'"([^"]+)"', raw)
    if literals:
        # Use the last literal — typically the filename/key part
        last = literals[-1].strip()
        # If it looks like just a path separator, skip it
        if last and last not in ('/', '\\', '//', '\\\\'):
            return last
    # No quoted literals — try splitting on ' + ' or '+'
    if ' + ' in raw or ('+' in raw and 'context.' in raw):
        parts = re.split(r'\s*\+\s*', raw)
        # Look for last part that has a file extension or doesn't look like a path dir
        for part in reversed(parts):
            part = part.strip().strip('"')
            if part and not part.endswith('/') and not part.endswith('\\') \
               and not part.startswith('context.'):
                return part
        # All parts are context vars — return last one
        last_part = parts[-1].strip().strip('"')
        return last_part if last_part else raw
    # No literals, no concatenation — return as-is (context var or plain name)
    return raw.strip('"').strip()


def is_context_var(v: str) -> bool:
    return "context." in v or v.startswith("((String)")


def resolve_context(value: str, ctx: dict) -> tuple[str, bool]:
    """
    Attempt to resolve a context.VARIABLE reference using the Prod context dict.
    Returns (resolved_value, is_ambiguous).
    """
    if not is_context_var(value):
        return (value, False)
    # Try to substitute all context.VAR occurrences
    pattern = re.compile(r'context\.(\w+)')
    ambiguous = False
    result = value
    for m in pattern.finditer(value):
        var_name = m.group(1)
        if var_name in ctx:
            resolved = strip_quotes(ctx[var_name])
            result = result.replace(m.group(0), resolved)
        else:
            ambiguous = True
    # If still has context. references it's still ambiguous
    if "context." in result:
        ambiguous = True
    return (result, ambiguous)


def get_params(node: ET.Element) -> dict:
    """Return a dict of {name: value} for all elementParameter children."""
    params = {}
    for ep in node.findall("elementParameter"):
        name = ep.get("name", "")
        val  = ep.get("value", "")
        if name:
            params[name] = val
    return params


def get_table_values(node: ET.Element, table_name: str, element_ref: str) -> list[str]:
    """Extract values from nested TABLE elementParameter entries."""
    values: list[str] = []
    for ep in node.findall("elementParameter"):
        if ep.get("name", "") != table_name:
            continue
        for ev in ep.findall("elementValue"):
            if ev.get("elementRef", "") == element_ref:
                values.append(ev.get("value", ""))
    return values


def platform_to_type(platform: str) -> str:
    """Map platform to lineage object type."""
    if platform in ("FILE", "SFTP"):
        return "FILE"
    if platform == "S3":
        return "S3_FILE"
    return "TABLE"


def first_nonempty(params: dict, *keys: str) -> str:
    """Return strip_quotes(params[key]) for the first key whose stripped value is non-empty."""
    for k in keys:
        v = strip_quotes(params.get(k, ""))
        if v:
            return v
    return ""


def extract_table_from_query(query: str) -> str:
    """
    Heuristic: extract the primary table name from a SQL SELECT statement.
    Returns empty string if nothing found.
    """
    if not query:
        return ""
    # Remove newlines/tabs
    q = re.sub(r'[\r\n\t]+', ' ', query).strip()
    # Find FROM clause
    m = re.search(r'\bFROM\s+([\w\."]+)', q, re.IGNORECASE)
    if m:
        tbl = m.group(1).strip('"').strip()
        return tbl
    return ""


def extract_schema_table(raw: str) -> tuple[str, str]:
    """
    Split 'schema.table' or 'schema'.'table' into (schema, table).
    If no schema prefix, returns ('NULL', raw).
    """
    raw = raw.strip().strip('"')
    if '.' in raw:
        parts = raw.split('.', 1)
        return (parts[0].strip('"').lower(), parts[1].strip('"').lower())
    return ("NULL", raw.lower())


def redshift_lower(s: str) -> str:
    return s.lower() if s and s != "NULL" else s


def infer_api_platform(value: str) -> str:
    """Infer API platform from endpoint/url-like value."""
    v = (value or "").lower()
    if "eloqua" in v:
        return "ELOQUA"
    if "gainsight" in v:
        return "GAINSIGHT"
    if "salesforce" in v or "force.com" in v:
        return "SALESFORCE"
    return "API"


def collect_prod_context(root: ET.Element) -> dict:
    """
    Collect context variable values from the 'Prod' (or first available) context.
    Returns {var_name: raw_value}.
    """
    ctx: dict = {}
    # First pass: Prod context
    for context_el in root.findall("context"):
        name = context_el.get("name", "")
        if name.lower() in ("prod", "production"):
            for cp in context_el.findall("contextParameter"):
                ctx[cp.get("name", "")] = cp.get("value", "")
            return ctx
    # Fallback: first context block
    first = root.find("context")
    if first is not None:
        for cp in first.findall("contextParameter"):
            ctx[cp.get("name", "")] = cp.get("value", "")
    return ctx


# ─────────────── per-component extraction ────────────────────

def extract_source_info(comp_name: str, params: dict, ctx: dict,
                        unique_name: str, label: str) -> tuple[str, str, str, bool, str]:
    """
    Returns (schema, object_name, platform, is_ambiguous, external_location).
    """
    platform = SOURCE_COMPONENTS.get(comp_name, "REDSHIFT")
    schema = "NULL"
    obj    = "NULL"
    ambig  = False
    ext_loc = "NULL"

    if comp_name in ("tSalesforceInput", "tSalesforceRecurrenceInput", "tGainsightInput"):
        raw = strip_quotes(params.get("MODULENAME", params.get("OBJECT_NAME", "")))        # Newer tSalesforceInput (v0.37+) stores module in PROPERTIES JSON and LABEL
        if not raw or is_context_var(raw):
            # Try LABEL first (contains clean module name like "Account")
            lbl_raw = strip_quotes(params.get("LABEL", ""))
            if lbl_raw:
                raw = lbl_raw
            else:
                # Fall back to PROPERTIES JSON moduleName.storedValue
                props = params.get("PROPERTIES", "")
                m_mod = re.search(r'"moduleName"\s*[^}]*?"storedValue"\s*:\s*"([^"]+)"', props)
                if m_mod:
                    raw = m_mod.group(1).strip('"')
        raw, a = resolve_context(raw, ctx)
        obj = raw or unique_name
        ambig = a or is_context_var(obj)
        schema = "SALESFORCE"
        ext_loc = "https://login.salesforce.com"

    elif comp_name in ("tRedshiftInput", "tDBInput", "tInput"):
        # Try direct table name first (use first_nonempty to skip empty-value params)
        tbl_raw = first_nonempty(params, "TABLENAME", "TABLE_NAME", "TABLE")
        if tbl_raw:
            tbl_raw, a = resolve_context(tbl_raw, ctx)
            ambig = a
        else:
            q_raw = strip_quotes(params.get("QUERY", params.get("MEMO_SQL", "")))
            q_raw, a = resolve_context(q_raw, ctx)
            tbl_raw = extract_table_from_query(q_raw)
            ambig = a or (not tbl_raw)

        sch_raw = first_nonempty(params, "SCHEMANAME", "SCHEMA_DB", "SCHEMA_NAME")
        sch_raw, a2 = resolve_context(sch_raw, ctx)
        ambig = ambig or a2

        if "." in tbl_raw:
            schema, obj = extract_schema_table(tbl_raw)
        else:
            schema = redshift_lower(sch_raw) if sch_raw else "NULL"
            obj    = redshift_lower(tbl_raw) if tbl_raw else (label or unique_name).lower()

        # Resolve schema from context if still NULL
        if schema == "NULL":
            ctx_schema = strip_quotes(ctx.get("connection_LRUSLake_Schema",
                          ctx.get("connection_Schema", "")))
            if ctx_schema:
                schema = ctx_schema.lower()

    elif comp_name == "tJDBCInput":
        # Schema from LABEL (connection name carries schema info)
        lbl = strip_quotes(params.get("LABEL", label or ""))
        schema_raw = strip_quotes(params.get("SCHEMA_DB", params.get("SCHEMA_NAME", "")))
        schema_raw, a = resolve_context(schema_raw, ctx)

        # Try parsing query from PROPERTIES JSON blob
        props_raw = params.get("PROPERTIES", params.get("PROPERTIES_RAW", ""))
        query_str = ""
        if props_raw:
            # Extract storedValue for query or sql keys
            for key in ["query.storedValue", "sql.storedValue"]:
                m = re.search(re.escape(f'"{key.split(".")[0]}"') +
                              r'[^}]*?"storedValue"\s*:\s*"([^"]+)"', props_raw)
                if m:
                    query_str = m.group(1).replace("\\n", " ").replace("\\t", " ")
                    break
            if not query_str:
                # Try simpler pattern
                m = re.search(r'"storedValue"\s*:\s*"(SELECT[^"]+)"', props_raw, re.IGNORECASE)
                if m:
                    query_str = m.group(1).replace("\\n", " ")

        tbl = extract_table_from_query(query_str) if query_str else ""
        if "." in tbl:
            schema, obj = extract_schema_table(tbl)
        elif tbl:
            schema = redshift_lower(schema_raw) if schema_raw else "NULL"
            obj    = redshift_lower(tbl)
            ambig  = a
        else:
            obj = redshift_lower(lbl) if lbl else unique_name.lower()
            schema = redshift_lower(schema_raw) if schema_raw else "NULL"
            ambig = True

        if schema == "NULL":
            ctx_schema = strip_quotes(ctx.get("connection_LRUSLake_Schema", ""))
            if ctx_schema:
                schema = ctx_schema.lower()

    elif comp_name in ("tOracleInput", "tMysqlInput"):
        tbl_raw = strip_quotes(params.get("TABLENAME", ""))
        sch_raw = strip_quotes(params.get("SCHEMANAME", ""))
        tbl_raw, a  = resolve_context(tbl_raw, ctx)
        sch_raw, a2 = resolve_context(sch_raw, ctx)
        obj    = tbl_raw.lower() if tbl_raw else unique_name.lower()
        schema = sch_raw.lower() if sch_raw else "NULL"
        ambig  = a or a2

    elif comp_name == "tS3Get":
        bucket = strip_quotes(params.get("BUCKET", ""))
        key    = strip_quotes(params.get("KEY", params.get("REMOTE_FILE", "")))
        bucket, a  = resolve_context(bucket, ctx)
        key,    a2 = resolve_context(key, ctx)
        obj    = f"{bucket}/{key}" if (bucket or key) else unique_name
        schema = "S3"
        ambig  = a or a2

    elif comp_name == "tAzureBlobInput":
        container = strip_quotes(params.get("CONTAINER", ""))
        blob      = strip_quotes(params.get("BLOB", ""))
        container, a  = resolve_context(container, ctx)
        blob,      a2 = resolve_context(blob, ctx)
        obj    = f"{container}/{blob}" if (container or blob) else unique_name
        schema = "AZURE"
        ambig  = a or a2

    elif comp_name == "tFTPGet":
        # Prefer remote host/folder + filemask over local temporary filename.
        host_raw = strip_quotes(params.get("FTP_HOST", ""))
        remote_raw = strip_quotes(params.get("REMOTEDIR", ""))
        filemask_raw = strip_quotes(params.get("FILEMASK", params.get("FILENAME", "")))
        filemask_expr = filemask_raw

        host_raw, a_h = resolve_context(host_raw, ctx)
        remote_raw, a_r = resolve_context(remote_raw, ctx)
        filemask_raw, a_f = resolve_context(filemask_raw, ctx)

        filemask_simple = simplify_filename(filemask_raw)
        filemask_simple = filemask_simple.strip('"').strip()
        remote_clean = remote_raw.strip('"').strip().strip("/")
        host_clean = host_raw.strip('"').strip()

        had_context_token = "context." in filemask_expr

        if not filemask_simple and had_context_token:
            # Keep variable identity when value is blank in the selected context.
            filemask_simple = filemask_expr.replace("context.", "").strip()

        if filemask_simple and not is_context_var(filemask_simple):
            file_part = Path(filemask_simple).name
        else:
            file_part = filemask_simple or unique_name

        path_parts = []
        if remote_clean:
            path_parts.append(remote_clean)
        if file_part:
            path_parts.append(file_part)
        remote_obj = "/".join(path_parts) if path_parts else unique_name

        if host_clean:
            schema = "SFTP"
            obj = f"{host_clean}/{remote_obj}" if remote_obj else host_clean
            ext_loc = f"sftp://{host_clean}/{remote_clean}" if remote_clean else f"sftp://{host_clean}"
        else:
            schema = "FILE"
            obj = remote_obj
            ext_loc = "NULL"

        ambig = a_h or a_r or a_f or had_context_token or is_context_var(file_part) or is_context_var(remote_obj)

    elif comp_name in ("tFileInputDelimited", "tFileInputJSON",
                       "tFileInputExcel"):
        raw = params.get("FILENAME", params.get("OUTPUT_FILE", ""))
        raw = raw.replace("&quot;", '"')
        # Simplify concatenation expressions before context resolve
        raw_simple = simplify_filename(raw)
        raw_simple, a = resolve_context(raw_simple, ctx)
        # Simplify again in case context resolution produced path + file patterns
        raw_simple = simplify_filename(raw_simple)
        if is_context_var(raw_simple):
            # strip to variable name only
            raw_simple = raw_simple.replace("context.", "").strip()
            ambig = True
        else:
            ambig = a
        obj    = Path(raw_simple).name if raw_simple and not is_context_var(raw_simple) else (raw_simple or unique_name)
        obj    = obj.strip('"').strip()
        schema = "FILE"
        ext_loc = "NULL"

    elif comp_name in ("tHTTPInput", "tRESTClient"):
        raw = strip_quotes(params.get("URL", params.get("RESOURCE_URI", "")))
        raw, a = resolve_context(raw, ctx)
        obj    = raw or unique_name
        platform = infer_api_platform(obj)
        schema = platform if platform != "API" else "API"
        ambig  = a or is_context_var(obj)
        ext_loc = obj

    else:
        # Unknown source: use label or unique_name
        for pname in SOURCE_TABLE_PARAMS:
            v = strip_quotes(params.get(pname, ""))
            if v:
                v, a = resolve_context(v, ctx)
                obj = v
                ambig = a
                break
        if obj == "NULL":
            obj = (label or unique_name)
        ambig = True

    return (schema, obj, platform, ambig, ext_loc)


def extract_target_info(comp_name: str, params: dict, ctx: dict,
                        unique_name: str, label: str) -> tuple[str, str, str, bool]:
    """
    Returns (schema, object_name, platform, is_ambiguous).
    """
    platform = TARGET_COMPONENTS.get(comp_name, "REDSHIFT")
    schema = "NULL"
    obj    = "NULL"
    ambig  = False

    if comp_name in ("tRedshiftOutput", "tDBOutput"):
        tbl_raw = first_nonempty(params, "TABLENAME", "TABLE_NAME", "TABLE")
        sch_raw = first_nonempty(params, "SCHEMANAME", "SCHEMA_NAME", "SCHEMA_DB")
        tbl_raw, a  = resolve_context(tbl_raw, ctx)
        sch_raw, a2 = resolve_context(sch_raw, ctx)

        if "." in tbl_raw:
            schema, obj = extract_schema_table(tbl_raw)
        else:
            schema = redshift_lower(sch_raw) if sch_raw else "NULL"
            obj    = redshift_lower(tbl_raw) if tbl_raw else unique_name.lower()
        ambig = a or a2

        if schema == "NULL":
            ctx_schema = strip_quotes(ctx.get("connection_LRUSLake_Schema", ""))
            if ctx_schema:
                schema = ctx_schema.lower()

    elif comp_name == "tRedshiftBulkExec":
        # TABLE (field=DBTABLE) is the primary param name in most versions;
        # TABLE_NAME / TABLENAME are fallbacks for older component versions.
        tbl_raw = first_nonempty(params, "TABLE", "TABLE_NAME", "TABLENAME")
        sch_raw = first_nonempty(params, "SCHEMA_NAME", "SCHEMANAME", "SCHEMA_DB")
        tbl_raw, a  = resolve_context(tbl_raw, ctx)
        sch_raw, a2 = resolve_context(sch_raw, ctx)

        if "." in tbl_raw:
            schema, obj = extract_schema_table(tbl_raw)
        else:
            schema = redshift_lower(sch_raw) if sch_raw else "NULL"
            obj    = redshift_lower(tbl_raw) if tbl_raw else unique_name.lower()
        ambig = a or a2

        if schema == "NULL":
            ctx_schema = strip_quotes(ctx.get("connection_LRUSLake_Schema", ""))
            if ctx_schema:
                schema = ctx_schema.lower()

    elif comp_name == "tJDBCOutput":
        tbl_raw = first_nonempty(params, "TABLE_NAME", "TABLENAME", "TABLE")
        sch_raw = first_nonempty(params, "SCHEMA_NAME", "SCHEMA_DB")
        tbl_raw, a  = resolve_context(tbl_raw, ctx)
        sch_raw, a2 = resolve_context(sch_raw, ctx)
        if "." in tbl_raw:
            schema, obj = extract_schema_table(tbl_raw)
        else:
            schema = redshift_lower(sch_raw) if sch_raw else "NULL"
            obj    = redshift_lower(tbl_raw) if tbl_raw else unique_name.lower()
        ambig  = a or a2
        if schema == "NULL":
            ctx_schema = strip_quotes(ctx.get("connection_LRUSLake_Schema", ""))
            if ctx_schema:
                schema = ctx_schema.lower()

    elif comp_name in ("tOracleOutput", "tMysqlOutput"):
        tbl_raw = strip_quotes(params.get("TABLENAME", ""))
        sch_raw = strip_quotes(params.get("SCHEMANAME", ""))
        tbl_raw, a  = resolve_context(tbl_raw, ctx)
        sch_raw, a2 = resolve_context(sch_raw, ctx)
        obj    = tbl_raw.lower() if tbl_raw else unique_name.lower()
        schema = sch_raw.lower() if sch_raw else "NULL"
        ambig  = a or a2

    elif comp_name == "tS3Put":
        bucket = strip_quotes(params.get("BUCKET", ""))
        key    = strip_quotes(params.get("KEY", params.get("REMOTE_FILE", "")))
        bucket, a  = resolve_context(bucket, ctx)
        key,    a2 = resolve_context(key, ctx)
        obj    = f"{bucket}/{key}" if (bucket or key) else unique_name
        schema = "S3"
        ambig  = a or a2

    elif comp_name == "tAzureBlobOutput":
        container = strip_quotes(params.get("CONTAINER", ""))
        blob      = strip_quotes(params.get("BLOB", ""))
        container, a  = resolve_context(container, ctx)
        blob,      a2 = resolve_context(blob, ctx)
        obj    = f"{container}/{blob}" if (container or blob) else unique_name
        schema = "AZURE"
        ambig  = a or a2

    elif comp_name in ("tFileOutputDelimited", "tFileOutputJSON", "tFTPPut"):
        raw = params.get("FILENAME", params.get("OUTPUT_FILE", ""))
        raw = raw.replace("&quot;", '"')
        raw_simple = simplify_filename(raw)
        raw_simple, a = resolve_context(raw_simple, ctx)
        raw_simple = simplify_filename(raw_simple)
        if is_context_var(raw_simple):
            raw_simple = raw_simple.replace("context.", "").strip()
            ambig = True
        else:
            ambig = a
        obj    = Path(raw_simple).name if raw_simple and not is_context_var(raw_simple) else (raw_simple or unique_name)
        obj    = obj.strip('"').strip()
        schema = "FILE"

    elif comp_name in ("tSalesforceOutput", "tGainsightOutput"):
        raw = strip_quotes(params.get("MODULENAME", params.get("OBJECT_NAME", "")))
        raw, a = resolve_context(raw, ctx)
        obj    = raw or unique_name
        schema = "SALESFORCE"
        ambig  = a or is_context_var(obj)

    else:
        for pname in TARGET_TABLE_PARAMS:
            v = strip_quotes(params.get(pname, ""))
            if v:
                v, a = resolve_context(v, ctx)
                obj = v
                ambig = a
                break
        if obj == "NULL":
            obj = (label or unique_name)
        ambig = True

    return (schema, obj, platform, ambig)


# ─────────────── main job parser ──────────────────────────────

def parse_item_file(item_path: Path, root_path: Path = WORKSPACE) -> list[dict]:
    """
    Parse a single Talend .item file and return a list of lineage row dicts.
    """
    rows: list[dict] = []

    try:
        tree = ET.parse(str(item_path))
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  [WARN] XML parse error in {item_path.name}: {e}", file=sys.stderr)
        return rows

    job_name = strip_version(item_path.stem)
    # Relative path from workspace root
    try:
        rel_path = str(item_path.relative_to(root_path)).replace("\\", "/")
    except ValueError:
        rel_path = str(item_path).replace("\\", "/")

    ctx = collect_prod_context(root)

    ftp_connections: dict[str, str] = {}
    for node in root.findall("node"):
        if node.get("componentName", "") != "tFTPConnection":
            continue
        params = get_params(node)
        conn_name = strip_quotes(params.get("UNIQUE_NAME", ""))
        host = strip_quotes(params.get("HOST", ""))
        host, _ = resolve_context(host, ctx)
        if conn_name:
            ftp_connections[conn_name] = host

    sources: list[dict] = []
    targets: list[dict] = []
    run_jobs: list[str] = []

    for node in root.findall("node"):
        comp_name   = node.get("componentName", "")
        unique_name = ""
        label       = ""
        params      = get_params(node)
        unique_name = strip_quotes(params.get("UNIQUE_NAME", comp_name))
        label       = strip_quotes(params.get("LABEL", ""))

        if comp_name in SOURCE_COMPONENTS:
            if comp_name == "tFTPGet":
                conn_name = strip_quotes(params.get("CONNECTION", ""))
                if conn_name and conn_name in ftp_connections:
                    params["FTP_HOST"] = ftp_connections[conn_name]
                masks = get_table_values(node, "FILES", "FILEMASK")
                if masks:
                    params["FILEMASK"] = masks[0]

            sch, obj, platform, ambig, ext_loc = extract_source_info(
                comp_name, params, ctx, unique_name, label)

            # Folder-level override: REST components in Eloqua jobs should be ELOQUA,
            # even when object text is generic (e.g., tRESTClient_3).
            if comp_name in ("tHTTPInput", "tRESTClient") and "/process/eloqua/" in rel_path.lower():
                if platform == "API":
                    platform = "ELOQUA"
                    sch = "ELOQUA"

            sources.append({
                "schema": sch, "object": obj, "platform": platform,
                "ambig": ambig, "ext_loc": ext_loc,
                "type": platform_to_type(platform),
            })

        elif comp_name in TARGET_COMPONENTS:
            sch, obj, platform, ambig = extract_target_info(
                comp_name, params, ctx, unique_name, label)
            targets.append({
                "schema": sch, "object": obj, "platform": platform,
                "ambig": ambig,
                "type": platform_to_type(platform),
            })

        elif comp_name == "tRunJob":
            # Orchestration only; not emitted as data movement lineage.
            child_job = strip_quotes(params.get("PROCESS",
                         params.get("JOB_NAME", params.get("PROCESS_NAME", ""))))
            if child_job:
                run_jobs.append(strip_version(child_job))

    # ── S3-based bulk load pattern: tS3Put + tRedshiftBulkExec ───────────────
    # Load jobs have two components:
    #   (1) tS3Put: uploads a local .dat file to S3
    #   (2) tRedshiftBulkExec: executes COPY from S3 into Redshift table
    # There are no explicit tFileInput components, so we synthesize:
    #   - FILE source (from tS3Put.FILE) to pair with tS3Put target (FILE->S3)
    #   - S3 source (from tS3Put.BUCKET/KEY) to pair with Redshift target (S3->REDSHIFT)
    # This captures the correct DAT->S3->REDSHIFT pipeline.
    if not sources:
        # Find tS3Put and tRedshiftBulkExec
        s3_put_target = None
        redshift_target = None
        for tgt in targets:
            if tgt["platform"] == "S3":
                s3_put_target = tgt
            elif tgt["platform"] == "REDSHIFT":
                redshift_target = tgt
        
        # If we have both S3 and Redshift targets (load job pattern):
        if s3_put_target and redshift_target:
            # Synthesize FILE source for the S3 upload
            for node in root.findall("node"):
                if node.get("componentName", "") != "tS3Put":
                    continue
                s3_params = get_params(node)
                raw_file = s3_params.get("FILE", "").replace("&quot;", '"')
                simplified = simplify_filename(raw_file)
                simplified, a_f = resolve_context(simplified, ctx)
                simplified = simplify_filename(simplified)
                if is_context_var(simplified):
                    simplified = simplified.replace("context.", "").strip()
                file_obj = Path(simplified).name if simplified else "FILE"
                file_obj = file_obj.strip('"').strip() or "FILE"
                
                sources.append({
                    "schema": "FILE", "object": file_obj, "platform": "FILE",
                    "ambig": True,   # path always context-resolved in load jobs
                    "ext_loc": "NULL",
                    "type": "FILE",
                    "pairs_with": "S3",  # FILE source pairs with S3 target only
                })
                break  # Only use first tS3Put
            
            # Synthesize S3 source for the Redshift COPY (use s3_put_target as template)
            sources.append({
                "schema": s3_put_target["schema"],
                "object": s3_put_target["object"],
                "platform": "S3",
                "ambig": True,   # path always context-resolved
                "ext_loc": "NULL",
                "type": "S3_FILE",
                "pairs_with": "REDSHIFT",  # S3 source pairs with REDSHIFT target only
            })

    # Only emit concrete source->target data movement rows.
    if not sources or not targets:
        return rows

    for src in sources:
        for tgt in targets:
            # Skip mismatched pairs in load jobs (FILE shouldn't pair with REDSHIFT directly)
            if "pairs_with" in src and src["pairs_with"] != tgt["platform"]:
                continue
            if "pairs_with" in tgt:
                tgt_pairs = tgt.get("pairs_with", "")
                if tgt_pairs and src["platform"] != tgt_pairs:
                    continue

            # Do not emit direct self-links. These are usually artifacts of
            # filename simplification or jobs that read/write the same file token.
            if (
                src["platform"] == tgt["platform"]
                and src["type"] == tgt["type"]
                and src["schema"] == tgt["schema"]
                and src["object"] == tgt["object"]
            ):
                continue

            ambig = "Yes" if (src["ambig"] or tgt["ambig"]) else "No"

            # Determine location type
            external_platforms = {"SALESFORCE", "S3", "AZURE", "API", "ORACLE", "MYSQL", "SFTP"}
            loc_type = "EXTERNAL" if (src["platform"] in external_platforms or
                                      tgt["platform"] in external_platforms) else "LOCAL"
            ext_loc = src.get("ext_loc", "NULL")

            row = {
                # Source = upstream (what is being read)
                "Source_Schema":       src["schema"],
                "Source_Object":       src["object"],
                "Source_Type":         src["type"],
                # Target = downstream (what is being written)
                "Target_Schema":       tgt["schema"],
                "Target_Object":       tgt["object"],
                "Target_Type":         tgt["type"],
                "Dependency_Type":     "DATA_FLOW",
                "Column_Or_Parameter": "*",
                "Schema_Bound":        "No",
                "External_Location":   ext_loc,
                "Is_Ambiguous":        ambig,
                "Location_Type":       loc_type,
                "Source_Platform":     src["platform"],
                "Target_Platform":     tgt["platform"],
                "Transform_Tool":      "TALEND",
                "Transform_Name":      job_name,
                "Transform_Path":      rel_path,
                "Source_Column":       "NULL",
            }
            rows.append(row)

    return rows


# ─────────────── deduplication key ───────────────────────────

def normalize_transform_path(path: str) -> str:
    """Normalize job path by stripping version suffix from filename.
    e.g. LRUS_BI/process/SalesForce/Fl_sf_LRUSLake_Account_Load_0.3.item
         → LRUS_BI/process/SalesForce/Fl_sf_LRUSLake_Account_Load.item
    """
    if not path:
        return path
    # Replace _{digit}.{digit}.item with .item
    return re.sub(r'_\d+\.\d+\.item$', '.item', path)


def row_key(row: dict) -> tuple:
    # Keep full Transform_Path so each .item version is represented in output.
    norm_path = row["Transform_Path"]
    return (
        row["Source_Schema"], row["Source_Object"], row["Source_Type"],
        row["Target_Schema"], row["Target_Object"], row["Target_Type"],
        row["Column_Or_Parameter"],
        row["Source_Platform"], row["Target_Platform"],
        row["Transform_Name"], norm_path,
        row["Source_Column"],
    )


# ─────────────── schedule-driven job discovery ────────────────

def load_schedule_jobs(schedule_csv: Path) -> set:
    """
    Read the schedule CSV and return the set of version-stripped job stems
    from the 'Job' column.
    e.g.  'Batch_ica_LRUSLake_All_0.1' -> 'Batch_ica_LRUSLake_All'
    """
    stems: set = set()
    if not schedule_csv.exists():
        print(f"[WARN] Schedule CSV not found: {schedule_csv}", file=sys.stderr)
        return stems
    with open(schedule_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            job_field = row.get("Job", "").strip()
            if job_field and job_field.lower() != "job":
                stems.add(strip_version(job_field))
    return stems


def get_runjob_names(item_path: Path) -> list:
    """
    Parse one .item file and return version-stripped names of all tRunJob sub-jobs.
    """
    names: list = []
    try:
        tree = ET.parse(str(item_path))
        root = tree.getroot()
        for node in root.findall("node"):
            if node.get("componentName", "") == "tRunJob":
                params = get_params(node)
                job_ref = strip_quotes(params.get("PROCESS",
                           params.get("JOB_NAME", params.get("PROCESS_NAME", ""))))
                if job_ref:
                    names.append(strip_version(job_ref))
    except Exception:
        pass
    return names


def build_item_index(process_root: Path) -> dict:
    """
    Build a mapping: version-stripped job stem -> list of matching .item Paths.
    Multiple versions of the same job (e.g. _0.1, _0.2) are all retained.
    """
    index: dict = {}
    for fp in process_root.rglob("*.item"):
        stem = strip_version(fp.stem)
        index.setdefault(stem, []).append(fp)
    return index


def expand_job_set(seed_stems: set, item_index: dict, max_depth: int = 4) -> set:
    """
    BFS-expand seed stems by tracing tRunJob chains up to max_depth levels.
    Turns 10 schedule seeds into the full Batch->Wf->Fl stem set.
    Returns the complete set of allowed job stems.
    """
    allowed = set(seed_stems)
    frontier = set(seed_stems)
    for _ in range(max_depth):
        next_frontier: set = set()
        for stem in frontier:
            for fp in item_index.get(stem, []):
                for child_stem in get_runjob_names(fp):
                    if child_stem not in allowed:
                        allowed.add(child_stem)
                        next_frontier.add(child_stem)
        if not next_frontier:
            break
        frontier = next_frontier
    return allowed


# ─────────────── entry point ─────────────────────────────────

def main() -> None:
    # 1. Discover ALL .item files in the process directory
    target_files: list = []
    for fp in LRUS_BI_PROCESS.rglob("*.item"):
        target_files.append(fp)
    target_files.sort()
    
    print(f"Talend .item files found: {len(target_files)}")
    print(f"Processing all files (no schedule filter)\n")

    # 5. Parse each scoped file
    all_rows: list = []
    files_ok    = 0
    files_err   = 0
    ambig_count = 0
    errors: list = []

    for fp in target_files:
        try:
            rows = parse_item_file(fp)
            all_rows.extend(rows)
            files_ok += 1
            ambig_count += sum(1 for r in rows if r["Is_Ambiguous"] == "Yes")
        except Exception as e:
            files_err += 1
            errors.append(f"{fp.name}: {e}")
            print(f"  [ERROR] {fp.name}: {e}", file=sys.stderr)

    # 6. Deduplicate
    seen: set = set()
    unique_rows: list = []
    for row in all_rows:
        k = row_key(row)
        if k not in seen:
            seen.add(k)
            unique_rows.append(row)

    duplicates_removed = len(all_rows) - len(unique_rows)

    # 7. Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(unique_rows)

    print()
    print("=" * 62)
    print("  Talend Lineage Parser (All .item Files) — Complete")
    print("=" * 62)
    print(f"  Files scanned:            {len(target_files)}")
    print(f"  Files parsed (OK):        {files_ok}")
    print(f"  Files with errors:        {files_err}")
    print(f"  Total relationships:      {len(all_rows)}")
    print(f"  Duplicates removed:       {duplicates_removed}")
    print(f"  Unique rows written:      {len(unique_rows)}")
    print(f"  Ambiguous rows:           {ambig_count}")
    print(f"  Output:                   {OUTPUT_FILE}")
    print("=" * 62)

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  • {e}")

    # 8. Append run record
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUTPUT_DIR / "rerun_log.txt", "a", encoding="utf-8") as lf:
        lf.write(
            f"\n## TALEND_ALL_FILES ({ts})\n"
            f"- Files: {len(target_files)}, OK: {files_ok}, "
            f"Rows: {len(unique_rows)}, Ambiguous: {ambig_count}, Errors: {files_err}\n"
            f"- Output: {OUTPUT_FILE}\n"
        )


if __name__ == "__main__":
    main()
