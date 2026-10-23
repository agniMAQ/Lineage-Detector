#!/usr/bin/env python3
"""
parse_python_lineage.py
───────────────────────
Parses Python scripts and Jupyter notebooks in the workspace to extract data
lineage from pandas, SQLAlchemy, psycopg2, boto3, and other ETL patterns.

Scopes: lrus_bi-master/, LRUS_BI/, Server2/, Server3/, and Server4/
Output: silver_agent/build/lineage-viewer/lineage-output/lineage_metadata_python.csv

Schema (18 columns, matching lineage-schema.instructions.md):
  Source_Schema, Source_Object, Source_Type, Target_Schema, Target_Object,
  Target_Type, Dependency_Type, Column_Or_Parameter, Schema_Bound,
  External_Location, Is_Ambiguous, Location_Type, Source_Platform,
  Target_Platform, Transform_Tool, Transform_Name, Transform_Path,
  Source_Column
"""

import csv
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

# ─────────────────────────── paths ───────────────────────────
WORKSPACE        = Path(r"c:\Repos\Scheduler")
LRUS_BI_MASTER   = WORKSPACE / "lrus_bi-master"
LRUS_BI          = WORKSPACE / "LRUS_BI"
SERVER2          = WORKSPACE / "Server2"
SERVER3          = WORKSPACE / "Server3"
SERVER4          = WORKSPACE / "Server4"
OUTPUT_DIR       = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output"
OUTPUT_FILE      = OUTPUT_DIR / "lineage_metadata_python.csv"

CSV_HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column",
]

# ─────────────────────────── patterns ───────────────────────────
# Patterns for data reads
PD_READ_SQL = re.compile(
    r"pd\.read_sql(?:_query|_table)?\s*\(\s*(?:r?['\"])([^'\"]+)(['\"])|([A-Za-z_][\w]*)\s*,",
    re.IGNORECASE | re.DOTALL
)
PD_READ_CSV = re.compile(
    r"pd\.read_csv\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)
PD_READ_PARQUET = re.compile(
    r"pd\.read_parquet\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)
PD_READ_JSON = re.compile(
    r"pd\.read_json\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)
PD_READ_EXCEL = re.compile(
    r"pd\.read_excel\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)
INLINE_DF_LITERAL = re.compile(
    r"\b([A-Za-z_][\w]*)\s*=\s*pd\.DataFrame\s*\(\s*(\{|\[)",
    re.IGNORECASE,
)
LOCAL_JSON_LITERAL = re.compile(
    r"['\"]([^'\"\n]+\.json)['\"]",
    re.IGNORECASE,
)

# Patterns for data writes
PD_TO_SQL = re.compile(
    r"\.to_sql\s*\(\s*(?:r?['\"])([^'\"]+)['\"]\s*,.*?schema\s*=\s*(?:r?['\"])([^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL
)
PD_TO_SQL_NO_SCHEMA = re.compile(
    r"\.to_sql\s*\(\s*(?:r?['\"])([^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL
)
PD_TO_SQL_CALL_WITH_SCHEMA = re.compile(
    r"\.to_sql\s*\(\s*(?:r?['\"])([^'\"]+)['\"].*?schema\s*=\s*(?:r?['\"])([^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)
PD_TO_SQL_CALL_NO_SCHEMA = re.compile(
    r"\.to_sql\s*\(\s*(?:r?['\"])([^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)
TO_SQL_CALL_GENERIC = re.compile(
    r"\.to_sql\s*\(\s*(.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
PD_TO_CSV = re.compile(
    r"\.to_csv\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)
PD_TO_PARQUET = re.compile(
    r"\.to_parquet\s*\(\s*(?:r?['\"])([^'\"]+)['\"](?:\s*,[^\)]*)?\)",
    re.IGNORECASE | re.DOTALL
)

# psycopg2 execute_values pattern with SQL object formatting
SQL_INSERT_FORMAT = re.compile(
    r"([A-Za-z_][\w]*)\s*=\s*sql\.SQL\(\s*['\"]INSERT\s+INTO\s+\{\}\.\{\}.*?['\"]\s*\)\s*\.format\(\s*"
    r"sql\.Identifier\(\s*([A-Za-z_][\w]*)\s*\)\s*,\s*sql\.Identifier\(\s*([A-Za-z_][\w]*)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
EXECUTE_VALUES_CALL = re.compile(
    r"extras\.execute_values\s*\(\s*[A-Za-z_][\w]*\s*,\s*([A-Za-z_][\w]*)\s*,",
    re.IGNORECASE,
)

# SQL patterns
READ_SQL_PATTERN = re.compile(
    r"(?:pd\.)?read_sql(?:_query|_table)?\s*\(",
    re.IGNORECASE
)
READ_SQL_CALL = re.compile(
    r"pd\.read_sql(?:_query|_table)?\s*\(\s*([A-Za-z_][\w]*|(?:r|f|rf|fr)?['\"].*?['\"])\s*,",
    re.IGNORECASE | re.DOTALL,
)
CURSOR_EXECUTE = re.compile(
    r"(?:cursor|conn|session)\s*\.\s*execute\s*\(",
    re.IGNORECASE
)

# SQL table extraction
FROM_CLAUSE = re.compile(
    r"\bFROM\s+(?:\")?([A-Za-z_][\w$]*(?:\.[\w$]+)?)",
    re.IGNORECASE
)
JOIN_CLAUSE = re.compile(
    r"\bJOIN\s+(?:\")?([A-Za-z_][\w$]*(?:\.[\w$]+)?)",
    re.IGNORECASE
)
INSERT_INTO = re.compile(
    r"INSERT\s+INTO\s+(?:\")?([A-Za-z_][\w$]*(?:\.[\w$]+)?)",
    re.IGNORECASE
)
SELECT_INTO = re.compile(
    r"SELECT\s+.*?\s+INTO\s+(?:\")?([A-Za-z_][\w$]*(?:\.[\w$]+)?)",
    re.IGNORECASE
)

# API and external system patterns (restored from old parser)
REQ_GET = re.compile(r"requests\.get\s*\(\s*(?:r|f|rf|fr)?['\"]([^'\"]+)['\"]", re.IGNORECASE)
REQ_POST = re.compile(r"requests\.post\s*\(\s*(?:r|f|rf|fr)?['\"]([^'\"]+)['\"]", re.IGNORECASE)

# SQL-level COPY/UNLOAD patterns (text-level extraction for S3-based flows)
COPY_FROM_S3 = re.compile(
    r"\bCOPY\s+([A-Za-z_][\w$]*(?:\.[\w$]+)?)\s+FROM\s+['\"]?(s3://[^\s'\"]+)['\"]?",
    re.IGNORECASE | re.MULTILINE
)
UNLOAD_TO_S3 = re.compile(
    r"\bUNLOAD\s*\(\s*SELECT\s+(.*?)\s+\).*?TO\s+['\"]?(s3://[^\s'\"]+)['\"]?",
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)

# Multiline execute SQL blocks, e.g. c.execute("""INSERT ... SELECT ...""")
EXECUTE_SQL_BLOCK = re.compile(
    r"(?:cursor|conn|session|c)\s*\.\s*execute\s*\(\s*(?:r|f|rf|fr)?([\"']{3})(.*?)\1\s*\)",
    re.IGNORECASE | re.DOTALL,
)

SQL_READ_KEYWORDS = ("SELECT", "FROM", "JOIN", "WITH")
SQL_WRITE_OR_DDL_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "DROP", "ALTER")

# ─────────────────────────── helpers ─────────────────────────────

def platform_from_url(url: str) -> str:
    """Determine platform from S3 or file path."""
    if url.startswith("s3://"):
        return "S3"
    return "FILE"


def api_platform_from_url(url: str) -> str:
    """Determine external platform from API URL."""
    u = (url or "").lower()
    # Handle interpolated Gong URL patterns seen in notebooks, e.g. f"{URL}/v2/calls".
    if any(token in u for token in ("/v2/calls", "/v2/settings/trackers")):
        return "GONG"
    if any(token in u for token in ("api.gong.io", "app.gong.io", "chorus.ai", "gong.io")):
        return "GONG"
    if any(token in u for token in ("api.linkedin.com", "linkedin.com", "licdn.com")):
        return "LINKEDIN"
    if any(token in u for token in ("customergauge.com",)):
        return "CG"
    if any(token in u for token in ("conductor.com",)):
        return "CONDUCTOR"
    if "gainsightcloud" in u:
        return "GAINSIGHT"
    if any(token in u for token in ("eloqua.com",)):
        return "ELOQUA"
    if any(token in u for token in ("salesforce.com", "force.com", "login.salesforce.com", "test.salesforce.com")):
        return "SALESFORCE"
    if any(token in u for token in ("management.azure.com", ".azure-api.net", "azure.com/")):
        return "AZURE"
    if any(token in u for token in ("google-analytics", "googleanalytics", "analytics.google", "googleapis.com/analytics")):
        return "GOOGLE_ANALYTICS"
    return "API"


def extract_literal_variables(code: str) -> dict:
    """Extract simple literal string assignments for placeholder interpolation."""
    vars_map = {}
    assign_re = re.compile(r"^\s*([A-Za-z_][\w]*)\s*=\s*['\"]([^'\"]+)['\"]\s*$", re.MULTILINE)
    for m in assign_re.finditer(code):
        name = m.group(1)
        val = m.group(2).strip()
        if not val:
            continue
        vars_map[name] = val
        vars_map[name.lower()] = val
        vars_map[name.upper()] = val
    return vars_map


def interpolate_placeholders(text: str, literal_vars: dict | None = None) -> str:
    """Interpolate {var} placeholders using known literal variable assignments."""
    if not text or not literal_vars:
        return text

    def repl(m):
        key = m.group(1)
        if key in literal_vars:
            return str(literal_vars[key])
        if key.lower() in literal_vars:
            return str(literal_vars[key.lower()])
        if key.upper() in literal_vars:
            return str(literal_vars[key.upper()])
        return m.group(0)

    return re.sub(r"\{([A-Za-z_][\w]*)\}", repl, text)


def resolve_token_literal(token: str, code: str, pos: int, literal_vars: dict | None = None) -> str:
    """Resolve a token that can be a quoted literal, f-string literal, or variable name."""
    t = (token or "").strip()
    if not t:
        return ""

    m_lit = re.match(r"^(?:r|f|rf|fr)?(['\"])(.*?)\1$", t, re.IGNORECASE | re.DOTALL)
    if m_lit:
        return interpolate_placeholders(m_lit.group(2), literal_vars).strip()

    if re.match(r"^[A-Za-z_][\w]*$", t):
        near = resolve_literal_var_before(code, t, pos)
        if near:
            return interpolate_placeholders(near, literal_vars).strip()
        if literal_vars:
            if t in literal_vars:
                return str(literal_vars[t]).strip()
            if t.lower() in literal_vars:
                return str(literal_vars[t.lower()]).strip()
            if t.upper() in literal_vars:
                return str(literal_vars[t.upper()]).strip()

    return ""


def is_write_api_endpoint(url: str) -> bool:
    """Check if API endpoint is for writing (POST to upsert/insert/update)."""
    u = (url or "").lower()
    return any(token in u for token in ("/upsert", "/insert", "/update", "/write", "/sync", "/bulk", "/ingest", "/load"))


def extract_sql_variables(code: str, literal_vars: dict | None = None) -> dict:
    """Extract SQL-like variable assignments, including simple string composition."""

    def _strip_quotes(token: str) -> str:
        t = token.strip()
        for q in ("'''", '"""', "'", '"'):
            if t.startswith(q) and t.endswith(q) and len(t) >= 2 * len(q):
                return t[len(q):-len(q)]
        return ""

    def _resolve_expr(expr: str, vars_map: dict) -> str:
        e = expr.strip().rstrip(";")
        lit = _strip_quotes(e)
        if lit:
            return interpolate_placeholders(lit, literal_vars)
        if e in vars_map:
            return vars_map[e]
        if "+" in e:
            parts = [p.strip() for p in e.split("+")]
            out = []
            had_resolved = False
            for p in parts:
                s = _strip_quotes(p)
                if s:
                    out.append(s)
                    had_resolved = True
                elif p in vars_map:
                    out.append(vars_map[p])
                    had_resolved = True
                else:
                    out.append("")
            rendered = "".join(out) if had_resolved else ""
            return interpolate_placeholders(rendered, literal_vars)
        return ""

    def _looks_sql(text: str) -> bool:
        u = (text or "").upper()
        return any(k in u for k in ("SELECT", "FROM", "JOIN", "WITH", "INSERT", "INTO", "DELETE", "UPDATE"))

    sql_vars = {}

    # First pass: multiline triple-quoted assignments.
    triple_assign = re.compile(
        r"^\s*([A-Za-z_][\w]*)\s*=\s*(?:r|f|rf|fr)?([\"']{3})(.*?)\2",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    for m in triple_assign.finditer(code):
        var_name = m.group(1)
        content = m.group(3)
        if _looks_sql(content):
            sql_vars[var_name] = content

    # Second pass: line-based assignments and appends.
    for raw_line in code.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        m_assign = re.match(r"^([A-Za-z_][\w]*)\s*=\s*(.+)$", line)
        if m_assign:
            var_name = m_assign.group(1)
            expr = m_assign.group(2)
            resolved = _resolve_expr(expr, sql_vars)
            if resolved and _looks_sql(resolved):
                sql_vars[var_name] = resolved
            continue

        m_append = re.match(r"^([A-Za-z_][\w]*)\s*\+=\s*(.+)$", line)
        if m_append:
            var_name = m_append.group(1)
            expr = m_append.group(2)
            resolved = _resolve_expr(expr, sql_vars)
            if resolved:
                existing = sql_vars.get(var_name, "")
                combined = existing + resolved
                if _looks_sql(combined):
                    sql_vars[var_name] = combined

    return sql_vars


def is_s3_path(path: str) -> bool:
    return path.startswith("s3://")


def extract_table_from_sql(sql_text: str) -> tuple:
    """Extract (schema, table) from SQL.
    Returns ('NULL', 'NULL') if not found.
    """
    # Try INSERT INTO first
    m = INSERT_INTO.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    # Try SELECT INTO
    m = SELECT_INTO.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    # Try FROM clause
    m = FROM_CLAUSE.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    return ("NULL", "NULL")


def resolve_sql_var_near_position(code: str, var_name: str, pos: int, fallback_vars: dict) -> str:
    """Resolve SQL variable value using nearest prior assignment in code."""
    segment = code[:pos]
    assign_re = re.compile(rf"^\s*{re.escape(var_name)}\s*(?:=|\+=)\s*(.+)$", re.IGNORECASE | re.MULTILINE)
    last_expr = None
    for m in assign_re.finditer(segment):
        last_expr = m.group(1).strip().rstrip(";")

    if last_expr:
        # Combine all literal pieces in the expression, e.g. sql = sql + " from ..."
        parts = []
        lit_re = re.compile(r"(?:r|f|rf|fr)?(['\"]{1,3})(.*?)\1", re.IGNORECASE)
        for lm in lit_re.finditer(last_expr):
            parts.append(lm.group(2))
        if parts:
            return "".join(parts)

    return fallback_vars.get(var_name, "")


def line_text_at_pos(code: str, pos: int) -> str:
    """Return the full line containing position pos."""
    start = code.rfind("\n", 0, pos)
    end = code.find("\n", pos)
    if start == -1:
        start = 0
    else:
        start += 1
    if end == -1:
        end = len(code)
    return code[start:end]


def resolve_literal_var_before(code: str, var_name: str, pos: int) -> str:
    """Resolve last literal string assignment to var_name before a position."""
    assign_re = re.compile(
        rf"^\s*{re.escape(var_name)}\s*=\s*['\"]([^'\"]+)['\"]\s*$",
        re.MULTILINE,
    )

    last_val = ""
    for m in assign_re.finditer(code[:pos]):
        last_val = m.group(1).strip()

    # Fallback to any literal assignment in file if nothing found before the position.
    if last_val:
        return last_val
    m_any = assign_re.search(code)
    return m_any.group(1).strip() if m_any else ""


def resolve_insert_target_before(code: str, query_var: str, pos: int) -> dict:
    """Resolve target table for a SQL insert query var using nearest prior definition."""
    last_match = None
    for m in SQL_INSERT_FORMAT.finditer(code[:pos]):
        if m.group(1) == query_var:
            last_match = m

    if not last_match:
        return {}

    schema_var = last_match.group(2)
    table_var = last_match.group(3)
    schema_val = resolve_literal_var_before(code, schema_var, last_match.start())
    table_val = resolve_literal_var_before(code, table_var, last_match.start())

    if not schema_val or not table_val:
        return {}

    return {
        "schema": schema_val.lower(),
        "object": table_val.lower(),
        "type": "TABLE",
        "platform": "REDSHIFT",
        "external": "NULL",
        "location": "LOCAL",
        "ambiguous": "No",
    }


def is_test_probe_read_sql(arg_name: str, sql_text: str, line_text: str) -> bool:
    """Heuristic guard to skip connectivity/test probe read_sql calls."""
    a = (arg_name or "").lower().strip()
    s = (sql_text or "").lower()
    l = (line_text or "").lower()

    # Explicit test/probe/check naming pattern.
    if a and re.search(r"(^|_)(test|probe|check)(_|$)", a):
        return True

    # DataFrame variable used for quick connection checks, e.g., test_df = pd.read_sql(...)
    if re.search(r"\b(test|probe|check)\w*\s*=\s*pd\.read_sql", l):
        return True

    # Lightweight query probes often use LIMIT 1 and should not produce lineage.
    if "limit 1" in s and re.search(r"\bselect\b", s):
        if re.search(r"\b(test|probe|check|conn|connection)\b", l + " " + a):
            return True

    return False


def is_write_api_endpoint(url: str) -> bool:
    """Check if API endpoint is for writing (POST to upsert/insert/update)."""
    u = (url or "").lower()
    return any(token in u for token in ("/upsert", "/insert", "/update", "/write", "/sync", "/bulk", "/ingest", "/load"))


def is_s3_path(path: str) -> bool:
    return path.startswith("s3://")


def extract_table_from_sql(sql_text: str) -> tuple:
    """Extract (schema, table) from SQL.
    Returns ('NULL', 'NULL') if not found.
    """
    # Try INSERT INTO first
    m = INSERT_INTO.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    # Try SELECT INTO
    m = SELECT_INTO.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    # Try FROM clause
    m = FROM_CLAUSE.search(sql_text)
    if m:
        full = m.group(1)
        if "." in full:
            parts = full.split(".", 1)
            return (parts[0].lower().strip('"'), parts[1].lower().strip('"'))
        return ("NULL", full.lower().strip('"'))
    
    return ("NULL", "NULL")


def read_notebook(nb_path: Path) -> str:
    """Extract Python code from Jupyter notebook."""
    try:
        with open(nb_path, encoding="utf-8") as f:
            nb = json.load(f)
        lines = []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") == "code":
                source = cell.get("source", [])
                if isinstance(source, list):
                    for s in source:
                        lines.append(s if s.endswith("\n") else s + "\n")
                else:
                    lines.append(source if source.endswith("\n") else source + "\n")
        return "".join(lines)
    except Exception as e:
        print(f"  [WARN] Error reading notebook {nb_path.name}: {e}", file=sys.stderr)
        return ""


def extract_statement_pairs(code: str, literal_vars: dict | None = None) -> list:
    """Extract direct source->target pairs from multiline execute SQL blocks."""
    pairs = []

    for match in EXECUTE_SQL_BLOCK.finditer(code):
        sql = interpolate_placeholders(match.group(2), literal_vars)
        if not sql:
            continue

        tgt_schema, tgt_table = extract_table_from_sql(sql)
        if tgt_table == "NULL":
            continue

        # Only treat INSERT/SELECT INTO as write targets.
        if not (INSERT_INTO.search(sql) or SELECT_INTO.search(sql)):
            continue

        src_tables = []
        for pat in (FROM_CLAUSE, JOIN_CLAUSE):
            for sm in pat.finditer(sql):
                full = sm.group(1)
                if "." in full:
                    s_schema, s_table = full.split(".", 1)
                    s_schema = s_schema.lower().strip('"')
                    s_table = s_table.lower().strip('"')
                else:
                    s_schema, s_table = "NULL", full.lower().strip('"')
                if s_table and s_table not in {"NULL", ""}:
                    src_tables.append((s_schema, s_table))

        # Deduplicate sources per statement.
        seen = set()
        for s_schema, s_table in src_tables:
            key = (s_schema, s_table)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((
                {
                    "schema": s_schema,
                    "object": s_table,
                    "type": "TABLE",
                    "platform": "REDSHIFT",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": "No",
                },
                {
                    "schema": tgt_schema,
                    "object": tgt_table,
                    "type": "TABLE",
                    "platform": "REDSHIFT",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": "No",
                },
            ))

    return pairs


def read_python(py_path: Path) -> str:
    """Read Python script."""
    try:
        return py_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"  [WARN] Error reading Python file {py_path.name}: {e}", file=sys.stderr)
        return ""


# ─────────────────────────── extraction ──────────────────────────

def extract_reads(code: str, transform_name: str, sql_vars: dict = None, literal_vars: dict | None = None) -> list:
    """Extract data read operations from code."""
    if sql_vars is None:
        sql_vars = {}
    
    reads = []

    # Inline DataFrame literals are legitimate upstream sources for notebook loads,
    # e.g. df = pd.DataFrame({'reference_date': ['2024-07-01']}).
    for m in INLINE_DF_LITERAL.finditer(code):
        df_var = (m.group(1) or "dataframe").strip().lower()
        reads.append({
            "schema": "NULL",
            "object": f"inline_dataframe_literal:{df_var}",
            "type": "FILE",
            "platform": "FILE",
            "external": "NULL",
            "location": "LOCAL",
            "ambiguous": "No",
        })

    # Detect local JSON ingestion patterns that do not use pd.read_json,
    # e.g. custom loaders with os.listdir(...endswith('.json')) + Path.open/json.load.
    has_json_loop = (
        re.search(r"os\.listdir\s*\(", code)
        and re.search(r"endswith\s*\(\s*['\"]\.json['\"]\s*\)", code, re.IGNORECASE)
    )
    has_json_open = (
        re.search(r"\.open\s*\(\s*['\"]r['\"]", code)
        and re.search(r"json\.load[s]?\s*\(", code)
    )
    if has_json_loop and has_json_open:
        reads.append({
            "schema": "NULL",
            "object": "*.json",
            "type": "FILE",
            "platform": "FILE",
            "external": "NULL",
            "location": "LOCAL",
            "ambiguous": "No",
        })
    
    # pandas read_sql across full code (handles multiline calls)
    for m in READ_SQL_CALL.finditer(code):
        arg = m.group(1).strip()
        sql_text = ""
        ambiguous = "No"
        call_line = line_text_at_pos(code, m.start())

        if arg and arg[0] in "'\"":
            sql_text = interpolate_placeholders(arg[1:-1], literal_vars)
        elif re.match(r"^[A-Za-z_][\w]*$", arg):
            sql_text = resolve_sql_var_near_position(code, arg, m.start(), sql_vars)
            sql_text = interpolate_placeholders(sql_text, literal_vars)
            # Variable-based SQL is deterministic if we can resolve it.
            ambiguous = "No" if sql_text else "Yes"

        if is_test_probe_read_sql(arg, sql_text, call_line):
            continue

        if sql_text:
            # Capture all sources participating in the query (FROM and JOIN tables).
            srcs = []
            for pat in (FROM_CLAUSE, JOIN_CLAUSE):
                for tm in pat.finditer(sql_text):
                    full = tm.group(1)
                    if "." in full:
                        schema, table = full.split(".", 1)
                        schema = schema.lower().strip('"')
                        table = table.lower().strip('"')
                    else:
                        schema, table = "NULL", full.lower().strip('"')
                    if table and table != "NULL":
                        srcs.append((schema, table))

            seen = set()
            for schema, table in srcs:
                key = (schema, table)
                if key in seen:
                    continue
                seen.add(key)
                reads.append({
                    "schema": schema,
                    "object": table,
                    "type": "TABLE",
                    "platform": "REDSHIFT",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": ambiguous,
                })

    # pandas read_sql (line-level fallback)
    for line in code.split("\n"):
        if "read_sql" in line.lower():
            # Extract SQL string or variable
            sql_match = re.search(r'read_sql(?:_query|_table)?\s*\(\s*(?:r?["\']([^"\']+)["\']|([A-Za-z_][\w]*))', line)
            if sql_match:
                sql_or_var = sql_match.group(1) or sql_match.group(2)
                ambiguous = "No"
                # Resolve variable if needed
                if sql_match.group(2):
                    var_name = sql_match.group(2)
                    if var_name in sql_vars:
                        sql_text = sql_vars[var_name]
                        ambiguous = "No"
                    else:
                        # Keep unresolved variable-based SQL explicitly ambiguous.
                        sql_text = ""
                        ambiguous = "Yes"
                else:
                    sql_text = sql_or_var

                arg_name = sql_match.group(2) or ""
                if is_test_probe_read_sql(arg_name, sql_text, line):
                    continue
                
                schema, table = extract_table_from_sql(sql_text)
                if table != "NULL":
                    reads.append({
                        "schema": schema,
                        "object": table,
                        "type": "TABLE",
                        "platform": "REDSHIFT",
                        "external": "NULL",
                        "location": "LOCAL",
                        "ambiguous": ambiguous,
                    })
        
        # pandas read_csv/parquet
        for pattern in [PD_READ_CSV, PD_READ_PARQUET, PD_READ_JSON, PD_READ_EXCEL]:
            for match in pattern.finditer(line):
                path = match.group(1)
                if path:
                    path = interpolate_placeholders(path, literal_vars)
                    p = platform_from_url(path)
                    reads.append({
                        "schema": "NULL",
                        "object": path,
                        "type": "FILE",
                        "platform": p,
                        "external": path if p == "S3" else "NULL",
                        "location": "EXTERNAL" if p == "S3" else "LOCAL",
                        "ambiguous": "No",
                    })

        # Explicit local JSON file literals used with custom json loaders.
        for jm in LOCAL_JSON_LITERAL.finditer(line):
            path = jm.group(1)
            if path and not path.lower().startswith(("http://", "https://", "s3://")):
                reads.append({
                    "schema": "NULL",
                    "object": path,
                    "type": "FILE",
                    "platform": "FILE",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": "No",
                })
        
        # requests.get() - API reads
        for url in REQ_GET.findall(line):
            platform = api_platform_from_url(url)
            reads.append({
                "schema": "NULL",
                "object": url,
                "type": "API_ENDPOINT",
                "platform": platform,
                "external": url,
                "location": "EXTERNAL",
                "ambiguous": "No",
            })
        
        # requests.post() - can be reads (query APIs)
        for url in REQ_POST.findall(line):
            platform = api_platform_from_url(url)
            reads.append({
                "schema": "NULL",
                "object": url,
                "type": "API_ENDPOINT",
                "platform": platform,
                "external": url,
                "location": "EXTERNAL",
                "ambiguous": "No",
            })
        
        # cursor.execute() with SELECT/FROM - these are reads
        if CURSOR_EXECUTE.search(line):
            sql_match = re.search(r"execute\s*\(\s*(?:r?['\"]([^'\"]+)['\"])", line)
            if sql_match:
                sql = sql_match.group(1)
                sql_upper = sql.upper()
                # Only add if this is genuinely read-only SQL.
                if (
                    FROM_CLAUSE.search(sql)
                    and not any(k in sql_upper for k in SQL_WRITE_OR_DDL_KEYWORDS)
                    and any(k in sql_upper for k in SQL_READ_KEYWORDS)
                ):
                    schema, table = extract_table_from_sql(sql)
                    if table != "NULL":
                        reads.append({
                            "schema": schema,
                            "object": table,
                            "type": "TABLE",
                            "platform": "REDSHIFT",
                            "external": "NULL",
                            "location": "LOCAL",
                            "ambiguous": "No",
                        })
    
    return reads


def extract_writes(code: str, transform_name: str, literal_vars: dict | None = None) -> list:
    """Extract data write operations from code."""
    writes = []

    # Detect multiline pandas to_sql calls across full code first.
    for m in PD_TO_SQL_CALL_WITH_SCHEMA.finditer(code):
        table = interpolate_placeholders((m.group(1) or "").strip(), literal_vars)
        schema = interpolate_placeholders((m.group(2) or "").strip(), literal_vars)
        if table == "{table}" and literal_vars and "STAGING_TABLE" in literal_vars:
            table = str(literal_vars["STAGING_TABLE"])
        if schema == "{schema}" and literal_vars and "STAGING_SCHEMA" in literal_vars:
            schema = str(literal_vars["STAGING_SCHEMA"])
        if table and schema:
            writes.append({
                "schema": schema.lower(),
                "object": table.lower(),
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
                "ambiguous": "No",
            })

    # Fallback: to_sql calls without explicit schema.
    for m in PD_TO_SQL_CALL_NO_SCHEMA.finditer(code):
        table = interpolate_placeholders((m.group(1) or "").strip(), literal_vars)
        window = code[m.start(): m.start() + 500]
        # Skip fallback when schema is explicitly provided in the same call.
        if "schema" in window.lower() and re.search(r"\bschema\s*=", window, re.IGNORECASE):
            continue
        if table:
            writes.append({
                "schema": "public",
                "object": table.lower(),
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
                "ambiguous": "No",
            })

    # Generic to_sql detection that supports variable/f-string table/schema args,
    # e.g. to_sql(f'{table_name}', ..., schema=schema_var)
    for m in TO_SQL_CALL_GENERIC.finditer(code):
        call_args = m.group(1) or ""
        first = re.match(
            r"\s*((?:[A-Za-z_][\w]*|(?:r|f|rf|fr)?['\"].*?['\"]))\s*,",
            call_args,
            re.IGNORECASE | re.DOTALL,
        )
        if not first:
            continue

        table_token = first.group(1)
        schema_m = re.search(
            r"\bschema\s*=\s*((?:[A-Za-z_][\w]*|(?:r|f|rf|fr)?['\"].*?['\"]))",
            call_args,
            re.IGNORECASE | re.DOTALL,
        )

        table_val = resolve_token_literal(table_token, code, m.start(), literal_vars)
        if not table_val:
            continue

        if schema_m:
            schema_val = resolve_token_literal(schema_m.group(1), code, m.start(), literal_vars)
            if not schema_val:
                continue
            writes.append({
                "schema": schema_val.lower(),
                "object": table_val.lower(),
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
                "ambiguous": "No",
            })
        else:
            writes.append({
                "schema": "public",
                "object": table_val.lower(),
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
                "ambiguous": "No",
            })
    
    for line in code.split("\n"):
        if line.strip().startswith("#"):
            continue
        
        # requests.post() to write endpoints - API writes
        for url in REQ_POST.findall(line):
            if is_write_api_endpoint(url):
                platform = api_platform_from_url(url)
                writes.append({
                    "schema": "NULL",
                    "object": url,
                    "type": "API_ENDPOINT",
                    "platform": platform,
                    "external": url,
                    "location": "EXTERNAL",
                    "ambiguous": "No",
                })
        
        # pandas to_sql (with schema) - single-line fallback
        match = PD_TO_SQL.search(line)
        if match:
            table = match.group(1)
            schema = match.group(2)
            writes.append({
                "schema": schema.lower(),
                "object": table.lower(),
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
                "ambiguous": "No",
            })
        # pandas to_sql (without schema - single-line fallback)
        elif ".to_sql(" in line.lower():
            match = PD_TO_SQL_NO_SCHEMA.search(line)
            if match:
                table = match.group(1)
                writes.append({
                    "schema": "public",
                    "object": table.lower(),
                    "type": "TABLE",
                    "platform": "REDSHIFT",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": "No",
                })
        
        # pandas to_csv/parquet
        for pattern in [PD_TO_CSV, PD_TO_PARQUET]:
            for match in pattern.finditer(line):
                path = match.group(1)
                if path:
                    path = interpolate_placeholders(path, literal_vars)
                    p = platform_from_url(path)
                    writes.append({
                        "schema": "NULL",
                        "object": path,
                        "type": "FILE",
                        "platform": p,
                        "external": path if p == "S3" else "NULL",
                        "location": "EXTERNAL" if p == "S3" else "LOCAL",
                        "ambiguous": "No",
                    })
        
        # cursor.execute() with INSERT/DELETE - these are writes
        if CURSOR_EXECUTE.search(line):
            sql_match = re.search(r"execute\s*\(\s*(?:r?['\"]([^'\"]+)['\"])", line)
            if sql_match:
                sql = sql_match.group(1)
                # Only add if it's an INSERT/DELETE (write)
                if INSERT_INTO.search(sql) or SELECT_INTO.search(sql):
                    schema, table = extract_table_from_sql(sql)
                    if table != "NULL":
                        writes.append({
                            "schema": schema,
                            "object": table,
                            "type": "TABLE",
                            "platform": "REDSHIFT",
                            "external": "NULL",
                            "location": "LOCAL",
                            "ambiguous": "No",
                        })

    # psycopg2 extras.execute_values(cur, insert_query, data_tuples)
    # Resolve each call against the nearest prior insert_query definition.
    for ev in EXECUTE_VALUES_CALL.finditer(code):
        query_var = ev.group(1)
        target = resolve_insert_target_before(code, query_var, ev.start())
        if target:
            writes.append(target)
    
    return writes


def extract_text_level_sources(code: str) -> list:
    """Extract sources from SQL-level operations like UNLOAD."""
    reads = []
    code_lower = code.lower()
    
    # UNLOAD (FROM redshift to S3)
    if "unload" in code_lower:
        for match in UNLOAD_TO_S3.finditer(code):
            try:
                sql_query = match.group(1)  # SELECT portion
                s3_path = match.group(2)
                schema, table = extract_table_from_sql(sql_query)
                if table != "NULL":
                    reads.append({
                        "schema": schema,
                        "object": table,
                        "type": "TABLE",
                        "platform": "REDSHIFT",
                        "external": "NULL",
                        "location": "LOCAL",
                        "ambiguous": "No",
                    })
            except Exception:
                pass
    
    return reads


def extract_text_level_targets(code: str) -> list:
    """Extract targets from SQL-level operations like COPY."""
    writes = []
    code_lower = code.lower()
    
    # COPY (FROM S3 to redshift)
    if "copy" in code_lower:
        for match in COPY_FROM_S3.finditer(code):
            try:
                table_ref = match.group(1)
                s3_path = match.group(2)
                if "." in table_ref:
                    schema, table = table_ref.split(".", 1)
                else:
                    schema, table = "public", table_ref
                writes.append({
                    "schema": schema.lower(),
                    "object": table.lower(),
                    "type": "TABLE",
                    "platform": "REDSHIFT",
                    "external": "NULL",
                    "location": "LOCAL",
                    "ambiguous": "No",
                })
            except Exception:
                pass
    
    return writes


# ─────────────────────────── main parser ──────────────────────────

def parse_file(file_path: Path, root: Path) -> list:
    """Parse a Python file or notebook and return lineage rows."""
    rows = []
    
    # Read code
    if file_path.suffix == ".ipynb":
        code = read_notebook(file_path)
    else:
        code = read_python(file_path)
    
    if not code:
        return rows
    
    literal_vars = extract_literal_variables(code)

    # Extract SQL variables first
    sql_vars = extract_sql_variables(code, literal_vars)
    
    # Extract reads and writes (line-level patterns)
    reads = extract_reads(code, file_path.stem, sql_vars, literal_vars)
    writes = extract_writes(code, file_path.stem, literal_vars)
    
    # Extract text-level sources/targets (COPY/UNLOAD SQL patterns)
    reads.extend(extract_text_level_sources(code))
    writes.extend(extract_text_level_targets(code))
    
    # Build rows for each (write, read) pair
    try:
        rel_path = str(file_path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel_path = str(file_path).replace("\\", "/")
    transform_name = file_path.stem

    # Statement-local SQL lineage pairs (prevents loss for multiline execute blocks).
    stmt_pairs = extract_statement_pairs(code, literal_vars)
    for read, write in stmt_pairs:
        if (
            read["schema"] == write["schema"]
            and read["object"] == write["object"]
            and read["type"] == write["type"]
        ):
            continue
        rows.append({
            "Source_Schema": read["schema"],
            "Source_Object": read["object"],
            "Source_Type": read["type"],
            "Target_Schema": write["schema"],
            "Target_Object": write["object"],
            "Target_Type": write["type"],
            "Dependency_Type": "DATA_FLOW",
            "Column_Or_Parameter": "*",
            "Schema_Bound": "No",
            "External_Location": read.get("external", "NULL"),
            "Is_Ambiguous": "No",
            "Location_Type": read.get("location", "LOCAL"),
            "Source_Platform": read["platform"],
            "Target_Platform": write["platform"],
            "Transform_Tool": "PYTHON",
            "Transform_Name": transform_name,
            "Transform_Path": rel_path,
            "Source_Column": "NULL",
        })
    
    for write in writes:
        for read in reads:
            if (
                read["schema"] == write["schema"]
                and read["object"] == write["object"]
                and read["type"] == write["type"]
            ):
                continue
            row = {
                "Source_Schema": read["schema"],
                "Source_Object": read["object"],
                "Source_Type": read["type"],
                "Target_Schema": write["schema"],
                "Target_Object": write["object"],
                "Target_Type": write["type"],
                "Dependency_Type": "DATA_FLOW",
                "Column_Or_Parameter": "*",
                "Schema_Bound": "No",
                "External_Location": read.get("external", "NULL"),
                "Is_Ambiguous": "Yes" if (write["ambiguous"] == "Yes" or read["ambiguous"] == "Yes") else "No",
                "Location_Type": read.get("location", "LOCAL"),
                "Source_Platform": read["platform"],
                "Target_Platform": write["platform"],
                "Transform_Tool": "PYTHON",
                "Transform_Name": transform_name,
                "Transform_Path": rel_path,
                "Source_Column": "NULL",
            }
            rows.append(row)
    
    # If only reads (no writes), skip
    # If only writes (no reads), also skip
    
    return rows


def row_key(row: dict) -> tuple:
    """Deduplication key matching Talend parser."""
    return (
        row["Source_Schema"], row["Source_Object"], row["Source_Type"],
        row["Target_Schema"], row["Target_Object"], row["Target_Type"],
        row["Column_Or_Parameter"],
        row["Source_Platform"], row["Target_Platform"],
        row["Transform_Name"], row["Transform_Path"],
        row["Source_Column"],
    )


# ─────────────────────────── entry point ──────────────────────────

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Discover Python/notebook files
    target_files = []
    for root in [LRUS_BI_MASTER, LRUS_BI, SERVER2, SERVER3, SERVER4]:
        if not root.exists():
            continue
        for fp in root.rglob("*"):
            if not fp.is_file():
                continue
            if fp.suffix.lower() not in {".py", ".ipynb"}:
                continue
            # Skip test files and common excludes
            parts = fp.parts
            if any(x in parts for x in ("test", "tests", "__pycache__", ".venv", "venv", ".ipynb_checkpoints")):
                continue
            if fp.name in ("setup.py", "conf.py", "conftest.py"):
                continue
            target_files.append(fp)
    
    target_files.sort()
    print(f"Python/Notebook files found: {len(target_files)}")
    
    # Parse each file
    all_rows = []
    files_ok = 0
    files_err = 0
    errors = []
    
    for fp in target_files:
        try:
            rows = parse_file(fp, WORKSPACE)
            all_rows.extend(rows)
            if rows:
                files_ok += 1
        except Exception as e:
            files_err += 1
            errors.append(f"{fp.name}: {e}")
            print(f"  [ERROR] {fp.name}: {e}", file=sys.stderr)
    
    # Deduplicate
    seen = set()
    unique_rows = []
    for row in all_rows:
        k = row_key(row)
        if k not in seen:
            seen.add(k)
            unique_rows.append(row)
    
    duplicates_removed = len(all_rows) - len(unique_rows)
    
    # Write output
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(unique_rows)
    
    # Count ambiguous
    ambig_count = sum(1 for r in unique_rows if r["Is_Ambiguous"] == "Yes")
    
    print()
    print("=" * 62)
    print("  Python Lineage Parser — Complete")
    print("=" * 62)
    print(f"  Files scanned:            {len(target_files)}")
    print(f"  Files with lineage:       {files_ok}")
    print(f"  Files with errors:        {files_err}")
    print(f"  Total relationships:      {len(all_rows)}")
    print(f"  Duplicates removed:       {duplicates_removed}")
    print(f"  Unique rows written:      {len(unique_rows)}")
    print(f"  Ambiguous rows:           {ambig_count}")
    print(f"  Output:                   {OUTPUT_FILE}")
    print("=" * 62)
    
    if errors:
        print("\nErrors:")
        for e in errors[:10]:
            print(f"  • {e}")
    
    # Append run record
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUTPUT_DIR / "rerun_log.txt", "a", encoding="utf-8") as lf:
        lf.write(
            f"\n## PYTHON_LINEAGE ({ts})\n"
            f"- Files: {len(target_files)}, OK: {files_ok}, "
            f"Rows: {len(unique_rows)}, Ambiguous: {ambig_count}, Errors: {files_err}\n"
        )


if __name__ == "__main__":
    main()
