#!/usr/bin/env python3
"""
parse_redshift_lineage.py
───────────────────────────
Parses Redshift SQL files (.sql, .txt, .ddl) and extensionless files to extract
data lineage from CREATE VIEW, CREATE TABLE AS SELECT, INSERT INTO, COPY, UNLOAD.

Scopes: lrus_bi-master/, Server2/, Server3/, and Server4/ (SQL-like .sql/.txt files and
extensionless files with SQL content), plus VIEW object_definition rows from
"Power BI - Tableau Reports Consolidated(Redshift Inventory).csv" (repo root) - this catches
Redshift VIEWs whose SQL only lives in that export and not as an on-disk file.
Output: silver_agent/build/lineage-viewer/lineage-output/lineage_metadata_redshift.csv

Schema (18 columns, matching lineage-schema.instructions.md):
  Source_Schema, Source_Object, Source_Type, Target_Schema, Target_Object,
  Target_Type, Dependency_Type, Column_Or_Parameter, Schema_Bound,
  External_Location, Is_Ambiguous, Location_Type, Source_Platform,
  Target_Platform, Transform_Tool, Transform_Name, Transform_Path,
  Source_Column
"""

import csv
import re
import sys
from pathlib import Path
from collections import defaultdict

# ─────────────────────────── paths ───────────────────────────
WORKSPACE        = Path(r"c:\Repos\Scheduler")
LRUS_BI_MASTER   = WORKSPACE / "lrus_bi-master"
SERVER2          = WORKSPACE / "Server2"
SERVER3          = WORKSPACE / "Server3"
SERVER4          = WORKSPACE / "Server4"
REDSHIFT_INVENTORY_CSV = WORKSPACE / "Power BI - Tableau Reports Consolidated(Redshift Inventory).csv"
OUTPUT_DIR       = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output"
OUTPUT_FILE      = OUTPUT_DIR / "lineage_metadata_redshift.csv"

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
# SQL anchors - identifies if file contains SQL
SQL_ANCHOR = re.compile(
    r"\b(create\s+(?:or\s+replace\s+)?view|create\s+table|insert\s+into|"
    r"with\b|select\b|drop\s+view|copy\b|unload\b)\b",
    re.IGNORECASE | re.MULTILINE
)

# Identifier patterns (quoted and unquoted SQL identifiers)
IDENT_PART = r'(?:"?[A-Za-z_][\w$]*"?)'
QUALIFIED_IDENT = rf'({IDENT_PART}(?:\.{IDENT_PART})?)'

# Target extraction (what is being created/inserted into)
CREATE_VIEW = re.compile(
    rf"create\s+(?:or\s+replace\s+)?view\s+{QUALIFIED_IDENT}\s*(?:\([^)]*\)\s*)?as",
    re.IGNORECASE
)
CREATE_TABLE_AS = re.compile(
    rf"create\s+table\s+{QUALIFIED_IDENT}\s+as",
    re.IGNORECASE
)
INSERT_INTO = re.compile(
    rf"insert\s+into\s+{QUALIFIED_IDENT}",
    re.IGNORECASE
)
COPY_INTO = re.compile(
    rf"copy\s+{QUALIFIED_IDENT}\s+from\s+['\"]?([^\s'\"]+)",
    re.IGNORECASE
)
UNLOAD = re.compile(
    r"unload\s*\(\s*select",
    re.IGNORECASE
)

# Source extraction (table references)
FROM_CLAUSE = re.compile(
    rf"\b(?:from|join)\s+{QUALIFIED_IDENT}",
    re.IGNORECASE
)

# CTE extraction
CTE_PATTERN = re.compile(
    r"(?:with|,)\s*([A-Za-z_][\w$]*)\s+as\s*\(",
    re.IGNORECASE
)

# ─────────────────────────── helpers ───────────────────────────

def strip_sql_comments(sql_text: str) -> str:
    """Remove block and line comments before regex parsing."""
    without_block = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
    without_line = re.sub(r"--.*?$", " ", without_block, flags=re.MULTILINE)
    return without_line


def normalize_ident(value: str) -> str:
    """Normalize a SQL identifier token by trimming wrappers and quotes."""
    return str(value).strip().strip('"').lower()


def split_target_ref(ref: str) -> tuple[str, str]:
    """Split schema.table target ref; default schema to public when omitted."""
    cleaned = normalize_ident(ref)
    if "." in cleaned:
        schema, table = cleaned.split(".", 1)
        return normalize_ident(schema), normalize_ident(table)
    return "public", cleaned

def is_sql_file(file_path: Path) -> bool:
    """Check if file is likely SQL based on extension or content."""
    sql_dirs = {"usage", "sap", "sales", "pricing", "product", "retention_2026",
                "licensing", "bi", "lake", "flat", "ai", "market", "kli"}

    if file_path.suffix.lower() in {".sql", ".ddl"}:
        return True
    
    if file_path.suffix.lower() == ".txt":
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            if SQL_ANCHOR.search(content):
                return True
        except Exception:
            pass

    # Support schema.object-style filenames that are SQL scripts but have nonstandard suffixes.
    # Example: usage.vl_view_actions_by_pub_full_year
    name_lower = file_path.name.lower()
    if "." in name_lower:
        prefix = name_lower.split(".", 1)[0]
        if prefix in sql_dirs:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                if SQL_ANCHOR.search(content):
                    return True
            except Exception:
                pass
    
    # Extensionless files in SQL directories
    if file_path.suffix == "":
        # Check if parent directory suggests SQL
        parent_name = file_path.parent.name.lower()
        if parent_name in sql_dirs:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                if SQL_ANCHOR.search(content):
                    return True
            except Exception:
                pass
    
    return False


def discover_sql_files(root: Path):
    """Find all SQL-like files in root directory."""
    sql_files = []
    
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        
        # Skip known non-SQL files
        if path.suffix.lower() in {".py", ".ipynb", ".json", ".csv", ".xlsx", ".json", 
                                    ".png", ".jpg", ".gif", ".zip", ".md"}:
            continue
        
        if is_sql_file(path):
            sql_files.append(path)
    
    return sql_files


def extract_ctes(sql_text: str) -> dict:
    """Extract CTE definitions and map each CTE to its underlying source tables."""
    sql_text = strip_sql_comments(sql_text)
    cte_map = {}
    
    # Find all CTE definitions: WITH cte_name AS ( ... )
    # Match pattern: (with|,) cte_name as (
    cte_start_pattern = re.compile(
        r"(?:with|,)\s*([A-Za-z_][\w$]*)\s+as\s*\(",
        re.IGNORECASE
    )
    
    for match in cte_start_pattern.finditer(sql_text):
        cte_name = match.group(1).lower()
        # Find the body of this CTE (from opening paren to matching closing paren)
        start_pos = match.end()
        paren_depth = 1
        end_pos = start_pos
        
        # Find matching closing paren
        for i in range(start_pos, len(sql_text)):
            if sql_text[i] == '(':
                paren_depth += 1
            elif sql_text[i] == ')':
                paren_depth -= 1
                if paren_depth == 0:
                    end_pos = i
                    break
        
        # Extract tables referenced in this CTE's body
        cte_body = sql_text[start_pos:end_pos]
        cte_sources = []
        
        for from_match in FROM_CLAUSE.finditer(cte_body):
            table_ref = from_match.group(1).lower()
            
            # Skip if it's another CTE (will be resolved recursively)
            if table_ref in cte_map or table_ref.startswith(("information_schema", "pg_catalog", "sys")):
                continue

            # Only keep fully-qualified schema.table sources.
            if "." not in table_ref:
                continue

            schema, table = table_ref.split(".", 1)
            
            schema = schema.lower()
            table = table.lower()
            
            cte_sources.append({
                "schema": schema,
                "object": table,
                "type": "TABLE",
            })
        
        cte_map[cte_name] = cte_sources
    
    return cte_map


def extract_sources(sql_text: str, cte_map: dict = None) -> list:
    """Extract source table references from SQL statement, resolving CTEs to real tables."""
    sql_text = strip_sql_comments(sql_text)
    if cte_map is None:
        cte_map = {}
    
    sources = []
    seen = set()
    
    for match in FROM_CLAUSE.finditer(sql_text):
        table_ref = normalize_ident(match.group(1))
        table_name_lower = table_ref.lower()
        
        # If it's a CTE reference, resolve to its underlying tables
        if table_name_lower in cte_map:
            for cte_source in cte_map[table_name_lower]:
                key = (cte_source["schema"], cte_source["object"])
                if key not in seen:
                    seen.add(key)
                    sources.append({
                        "schema": cte_source["schema"],
                        "object": cte_source["object"],
                        "type": "TABLE",
                        "platform": "REDSHIFT",
                        "external": "NULL",
                        "location": "LOCAL",
                    })
            continue

        # Only emit fully-qualified schema.table sources.
        if "." not in table_ref:
            continue
        
        # Skip system schemas
        if table_name_lower.startswith(("information_schema", "pg_catalog", "sys")):
            continue
        
        # Parse schema.table
        schema, table = table_ref.split(".", 1)
        schema = normalize_ident(schema)
        table = normalize_ident(table)
        
        key = (schema, table)
        if key not in seen:
            seen.add(key)
            sources.append({
                "schema": schema,
                "object": table,
                "type": "TABLE",
                "platform": "REDSHIFT",
                "external": "NULL",
                "location": "LOCAL",
            })
    
    return sources


def _relative_path(file_path: Path, root: Path) -> str:
    try:
        return str(file_path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(file_path).replace("\\", "/")


def parse_statement(statement: str,
                    file_path: Path,
                    file_name: str,
                    root: Path = WORKSPACE) -> list:
    """Parse a single SQL statement and extract lineage rows."""
    rows = []
    statement_lower = statement.lower()
    
    # Trim for easier processing
    statement = statement.strip()
    cte_map = extract_ctes(statement)
    
    # Try CREATE VIEW
    match = CREATE_VIEW.search(statement)
    if match:
        schema, table = split_target_ref(match.group(1))
        sources = extract_sources(statement, cte_map)
        for source in sources:
            rows.append({
                "source_schema": source["schema"],
                "source_object": source["object"],
                "source_type": "TABLE",
                "target_schema": schema,
                "target_object": table,
                "target_type": "VIEW",
                "transform_name": file_name,
                "transform_path": _relative_path(file_path, root),
            })
        return rows
    
    # Try CREATE TABLE AS SELECT
    match = CREATE_TABLE_AS.search(statement)
    if match:
        schema, table = split_target_ref(match.group(1))
        sources = extract_sources(statement, cte_map)
        for source in sources:
            rows.append({
                "source_schema": source["schema"],
                "source_object": source["object"],
                "source_type": "TABLE",
                "target_schema": schema,
                "target_object": table,
                "target_type": "TABLE",
                "transform_name": file_name,
                "transform_path": _relative_path(file_path, root),
            })
        return rows
    
    # Try INSERT INTO
    match = INSERT_INTO.search(statement)
    if match:
        schema, table = split_target_ref(match.group(1))
        sources = extract_sources(statement, cte_map)
        for source in sources:
            rows.append({
                "source_schema": source["schema"],
                "source_object": source["object"],
                "source_type": "TABLE",
                "target_schema": schema,
                "target_object": table,
                "target_type": "TABLE",
                "transform_name": file_name,
                "transform_path": _relative_path(file_path, root),
            })
        return rows
    
    # Try COPY (S3 → Redshift)
    match = COPY_INTO.search(statement)
    if match:
        schema, table = split_target_ref(match.group(1))
        s3_path = match.group(2)
        rows.append({
            "source_schema": "NULL",
            "source_object": s3_path,
            "source_type": "S3_FILE",
            "target_schema": schema,
            "target_object": table,
            "target_type": "TABLE",
            "transform_name": file_name,
            "transform_path": _relative_path(file_path, root),
            "external": s3_path,
        })
        return rows
    
    # Try UNLOAD (Redshift → S3)
    if UNLOAD.search(statement):
        # Extract S3 path from UNLOAD statement
        s3_match = re.search(r"to\s+['\"]?([^\s'\"]+s3[^\s'\"]+)", statement, re.IGNORECASE)
        if s3_match:
            s3_path = s3_match.group(1).strip("'\"")
            # Try to find source tables
            sources = extract_sources(statement, cte_map)
            for source in sources:
                rows.append({
                    "source_schema": source["schema"],
                    "source_object": source["object"],
                    "source_type": "TABLE",
                    "target_schema": "NULL",
                    "target_object": s3_path,
                    "target_type": "S3_FILE",
                    "transform_name": file_name,
                    "transform_path": _relative_path(file_path, root),
                    "external": s3_path,
                })
    
    return rows


def _table_name_from_object_name(object_name: str, schema: str) -> str:
    """`object_name` is `<schema>.<table>` - strip the known schema prefix if present, else
    fall back to the part after the first dot, else the whole value."""
    if "." in object_name:
        prefix, rest = object_name.split(".", 1)
        return rest.strip()
    return object_name.strip()


def load_view_definitions_from_inventory(inventory_csv: Path = REDSHIFT_INVENTORY_CSV) -> list:
    """Load VIEW rows (object_type == VIEW, non-empty object_definition) from the Redshift
    inventory export. This is the same source silver_agent's
    redshift_view_definition_inventory.py uses to catch VIEWs whose SQL only lives in this
    CSV, not as an on-disk .sql/.txt file - see _common.py's redshift_inventory_csv_path().
    Returns rows: {"schema", "table_name", "object_name", "sql_text"}.
    """
    if not inventory_csv.exists():
        return []

    out_rows = []
    with inventory_csv.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            object_type = (row.get("object_type") or "").strip().upper()
            if object_type != "VIEW":
                continue
            sql_text = (row.get("object_definition") or "").strip()
            if not sql_text:
                continue
            schema = (row.get("Schema") or "").strip().lower()
            object_name = (row.get("object_name") or "").strip()
            table_name = _table_name_from_object_name(object_name, schema)
            if not table_name:
                continue
            out_rows.append({
                "schema": schema,
                "table_name": table_name.lower(),
                "object_name": object_name,
                "sql_text": sql_text,
            })
    return out_rows


def build_csv_row(parsed_row: dict) -> list:
    """Build 18-column CSV row from parsed statement."""
    external = parsed_row.get("external", "NULL")
    location = "EXTERNAL" if external != "NULL" else "LOCAL"
    
    return [
        parsed_row.get("source_schema", "NULL"),
        parsed_row.get("source_object", ""),
        parsed_row.get("source_type", "TABLE"),
        parsed_row.get("target_schema", "NULL"),
        parsed_row.get("target_object", ""),
        parsed_row.get("target_type", "TABLE"),
        "DATA_FLOW",
        "*",
        "No",
        external,
        "No",
        location,
        "REDSHIFT",
        "REDSHIFT" if location == "LOCAL" else "S3",
        "SQL",
        parsed_row.get("transform_name", ""),
        parsed_row.get("transform_path", ""),
        "NULL",
    ]


def main(output_csv: Path = OUTPUT_FILE, replace: bool = False):
    """Main parser execution."""
    # Prepare output
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    
    if replace and output_csv.exists():
        output_csv.unlink()
    
    # Write header if new
    if not output_csv.exists():
        with output_csv.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
    
    # Discover and process SQL files across all in-scope roots
    sql_files = []
    for root in (LRUS_BI_MASTER, SERVER2, SERVER3, SERVER4):
        if root.exists():
            sql_files.extend(discover_sql_files(root))
    
    print(f"SQL-like files found: {len(sql_files)}")
    
    total_rows = 0
    files_processed = 0
    errors = 0
    
    existing_keys = set()
    
    # Load existing rows for deduplication
    if output_csv.exists():
        with output_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (
                    row["Source_Schema"],
                    row["Source_Object"],
                    row["Source_Type"],
                    row["Target_Schema"],
                    row["Target_Object"],
                    row["Target_Type"],
                    row["Column_Or_Parameter"],
                    row["Source_Column"],
                )
                existing_keys.add(key)
    
    # Process each file
    new_rows = []
    
    for sql_file in sql_files:
        try:
            content = sql_file.read_text(encoding="utf-8", errors="ignore")
            content = strip_sql_comments(content)
            
            if not SQL_ANCHOR.search(content):
                continue
            
            files_processed += 1
            file_name = sql_file.stem
            
            # Split statements on semicolon
            statements = [s.strip() for s in content.split(";") if s.strip()]
            
            for statement in statements:
                if not SQL_ANCHOR.search(statement):
                    continue
                
                try:
                    parsed_rows = parse_statement(statement, sql_file, file_name)
                    
                    for row in parsed_rows:
                        key = (
                            row.get("source_schema", "NULL"),
                            row.get("source_object", ""),
                            row.get("source_type", "TABLE"),
                            row.get("target_schema", "NULL"),
                            row.get("target_object", ""),
                            row.get("target_type", "TABLE"),
                            "*",
                            "NULL",
                        )
                        
                        if key not in existing_keys:
                            existing_keys.add(key)
                            csv_row = build_csv_row(row)
                            new_rows.append(csv_row)
                            total_rows += 1
                except Exception as e:
                    print(f"  [ERROR] {sql_file.name}: {e}", file=sys.stderr)
                    errors += 1
        
        except Exception as e:
            print(f"  [ERROR] Reading {sql_file.name}: {e}", file=sys.stderr)
            errors += 1
    
    # Append new rows
    if new_rows:
        with output_csv.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(new_rows)
    
    # Process VIEW definitions from the Redshift inventory CSV export (catches VIEWs whose
    # SQL only lives in that export, not as an on-disk .sql/.txt file).
    inventory_views = load_view_definitions_from_inventory()
    inventory_rows_added = 0
    inventory_new_rows = []
    inventory_transform_path = str(REDSHIFT_INVENTORY_CSV.relative_to(WORKSPACE)).replace("\\", "/")

    for view_row in inventory_views:
        try:
            statement = strip_sql_comments(view_row["sql_text"])
            cte_map = extract_ctes(statement)
            sources = extract_sources(statement, cte_map)

            for source in sources:
                row = {
                    "source_schema": source["schema"],
                    "source_object": source["object"],
                    "source_type": "TABLE",
                    "target_schema": view_row["schema"] or "public",
                    "target_object": view_row["table_name"],
                    "target_type": "VIEW",
                    "transform_name": view_row["object_name"],
                    "transform_path": inventory_transform_path,
                }
                key = (
                    row["source_schema"], row["source_object"], row["source_type"],
                    row["target_schema"], row["target_object"], row["target_type"],
                    "*", "NULL",
                )
                if key not in existing_keys:
                    existing_keys.add(key)
                    inventory_new_rows.append(build_csv_row(row))
                    inventory_rows_added += 1
        except Exception as e:
            print(f"  [ERROR] inventory view {view_row.get('object_name')}: {e}", file=sys.stderr)
            errors += 1

    if inventory_new_rows:
        with output_csv.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(inventory_new_rows)
    
    print()
    print("=" * 70)
    print("  Redshift SQL Lineage Parser — Complete")
    print("=" * 70)
    print(f"  SQL files scanned:      {len(sql_files)}")
    print(f"  Files with valid SQL:   {files_processed}")
    print(f"  Lineage rows appended:  {total_rows}")
    print(f"  Inventory VIEWs found:  {len(inventory_views)}")
    print(f"  Inventory rows appended:{inventory_rows_added}")
    print(f"  Parsing errors:         {errors}")
    print(f"  Output:                 {output_csv}")
    print("=" * 70)


if __name__ == "__main__":
    main(replace=True)
