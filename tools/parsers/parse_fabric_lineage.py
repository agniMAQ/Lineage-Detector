#!/usr/bin/env python3
"""
parse_fabric_lineage.py
───────────────────────
Extracts data lineage from Fabric artifacts in a local repository:

    * Bronze ingestion   discovered config files + Bronze/Notebooks
    * Silver notebooks   Silver/Notebooks/**/*.Notebook/notebook-content.py
    * Gold notebooks     Gold/Notebooks/**/*.Notebook/notebook-content.py
    * Data pipelines     **/*.DataPipeline/pipeline-content.json
    * Semantic models    **/*.SemanticModel/definition/tables/*.tmdl
    * Power BI reports   **/*.Report/definition.pbir (semantic model attachment)

When a repository is supplied, outputs are written to <repository>/lineage-output/:
  lineage_metadata_fabric_bronze.csv
  lineage_metadata_fabric_silver.csv
  lineage_metadata_fabric_gold.csv
    lineage_metadata_fabric_pipelines.csv
  lineage_metadata_fabric_semantic_models.csv
  lineage_metadata_fabric_reports.csv
  lineage_metadata_fabric_consolidated.csv   (all merged)

Row direction follows the existing lineage-viewer convention:
  Source_* = the upstream object read, Target_* = the artifact that is produced.

Gold-lakehouse shortcuts (gold_lh.Lakehouse/shortcuts.metadata.json) are resolved back to the
Bronze/Silver table they point at, so a read of e.g. `gold.sf_user` is reported as `lake.sf_user`.
"""

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ─────────────────────────── paths ───────────────────────────
PARSERS_DIR = Path(__file__).resolve().parent
AGENTS_ROOT = PARSERS_DIR.parents[3]           # LRUS_BI_AI_Agents
WORKSPACE = PARSERS_DIR.parents[4]             # repo root (Scheduler)

FABRIC_ROOT = WORKSPACE / "LRUS_BI_Fabric"
BRONZE_NOTEBOOKS = FABRIC_ROOT / "Bronze" / "Notebooks"
SILVER_NOTEBOOKS = FABRIC_ROOT / "Silver" / "Notebooks"
GOLD_NOTEBOOKS = FABRIC_ROOT / "Gold" / "Notebooks"
SEMANTIC_MODELS = FABRIC_ROOT / "Semantic Models"
REPORTS_ROOT = FABRIC_ROOT
PIPELINES_ROOT = FABRIC_ROOT
SOURCES_CONFIG = WORKSPACE / "Useful artifacts" / "sources_config.json"
STATIC_FILES_CONFIG = WORKSPACE / "Useful artifacts" / "static_files_config.json"
REDSHIFT_DIRECT_CONFIG = WORKSPACE / "Useful artifacts" / "redshift_direct_tables.json"
SHORTCUT_METADATA = sorted(FABRIC_ROOT.rglob("*.Lakehouse/shortcuts.metadata.json"))
BRONZE_NOTEBOOK_ROOTS = [BRONZE_NOTEBOOKS]
SILVER_NOTEBOOK_ROOTS = [SILVER_NOTEBOOKS]
GOLD_NOTEBOOK_ROOTS = [GOLD_NOTEBOOKS]

OUTPUT_DIR = AGENTS_ROOT / "build" / "lineage-viewer" / "lineage-output"
BRONZE_CSV = OUTPUT_DIR / "lineage_metadata_fabric_bronze.csv"
SILVER_CSV = OUTPUT_DIR / "lineage_metadata_fabric_silver.csv"
GOLD_CSV = OUTPUT_DIR / "lineage_metadata_fabric_gold.csv"
PIPELINES_CSV = OUTPUT_DIR / "lineage_metadata_fabric_pipelines.csv"
SEMANTIC_CSV = OUTPUT_DIR / "lineage_metadata_fabric_semantic_models.csv"
REPORTS_CSV = OUTPUT_DIR / "lineage_metadata_fabric_reports.csv"
CONSOLIDATED_CSV = OUTPUT_DIR / "lineage_metadata_fabric_consolidated.csv"

CSV_HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column",
]

# ─────────────────────────── lakehouse → platform ───────────────────────────
LAKEHOUSE_PLATFORM = {
    "bronze_lh": "FABRIC_BRONZE",
    "lh_bronze_lh": "FABRIC_BRONZE",
    "lh_bronze_redshift": "FABRIC_BRONZE",
    "silver_lh": "FABRIC_SILVER",
    "lh_silver_lh": "FABRIC_SILVER",
    "gold_lh": "FABRIC_GOLD",
    "lh_gold_lrus": "FABRIC_GOLD",
}

# f-string config variables used inside table references
LAKEHOUSE_VARS = {
    "{BRONZE_LH_NAME}": "Bronze_LH",
    "{SILVER_LH_NAME}": "Silver_LH",
    "{GOLD_LH_NAME}": "lh_gold_lrus",
    "{BRONZE_LH}": "Bronze_LH",
    "{SILVER_LH}": "Silver_LH",
    "{GOLD_LH}": "lh_gold_lrus",
}

DEFAULT_LAKEHOUSE_PLATFORM = dict(LAKEHOUSE_PLATFORM)
DEFAULT_LAKEHOUSE_VARS = dict(LAKEHOUSE_VARS)

CONFIG_FILE_NAMES = {
    "sources": ("sources_config.json",),
    "static_files": ("static_files_config.json",),
    "redshift_direct": ("redshift_direct_tables.json",),
}

IGNORED_DISCOVERY_PARTS = {
    ".git", ".venv", "node_modules", "lineage-output", "__pycache__",
}


@dataclass(frozen=True)
class RepositoryLayout:
    repo_root: Path
    artifact_root: Path
    bronze_notebooks: tuple
    silver_notebooks: tuple
    gold_notebooks: tuple
    pipelines_root: Path
    semantic_models_root: Path
    reports_root: Path
    sources_config: Path
    static_files_config: Path
    redshift_direct_config: Path
    shortcut_metadata: tuple
    output_dir: Path
    profile_path: Optional[Path]
    lakehouse_platforms: dict
    lakehouse_variables: dict
    lakehouse_files_url_template: str
    warnings: tuple


def _profile_path(repo_root: Path, requested: Optional[Path]) -> Optional[Path]:
    if requested:
        path = requested if requested.is_absolute() else repo_root / requested
        if not path.is_file():
            raise FileNotFoundError(f"Lineage configuration not found: {path}")
        return path.resolve()
    default = repo_root / "lineage.config.json"
    return default.resolve() if default.is_file() else None


def _load_profile(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    try:
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in lineage configuration {path}: {exc}") from exc
    if not isinstance(profile, dict):
        raise ValueError(f"Lineage configuration must contain a JSON object: {path}")
    return profile


def _resolve_repo_path(repo_root: Path, value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else repo_root / path).resolve()


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part.casefold() in IGNORED_DISCOVERY_PARTS for part in parts)


def _configured_paths(repo_root: Path, value) -> list[Path]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError("Configured repository paths must be strings or lists of strings")
    return [_resolve_repo_path(repo_root, item) for item in values]


def _discover_layer_roots(artifact_root: Path, layer: str) -> list[Path]:
    roots = []
    for path in artifact_root.rglob("*"):
        if not path.is_dir() or path.name.casefold() != "notebooks":
            continue
        if _is_ignored(path, artifact_root):
            continue
        relative_parts = {part.casefold() for part in path.relative_to(artifact_root).parts[:-1]}
        if layer.casefold() in relative_parts:
            roots.append(path.resolve())
    return sorted(set(roots), key=lambda item: item.as_posix().casefold())


def _discover_named_file(artifact_root: Path, names: tuple[str, ...]) -> Optional[Path]:
    wanted = {name.casefold() for name in names}
    matches = sorted(
        (path.resolve() for path in artifact_root.rglob("*")
         if path.is_file()
         and path.name.casefold() in wanted
         and not _is_ignored(path, artifact_root)),
        key=lambda item: item.as_posix().casefold(),
    )
    if len(matches) > 1:
        paths = "\n  ".join(str(path) for path in matches)
        raise ValueError(
            f"Multiple files match {', '.join(names)}. Set the path in lineage.config.json:\n  {paths}"
        )
    return matches[0] if matches else None


def _configured_or_discovered_file(repo_root: Path, artifact_root: Path,
                                   config_files: dict, key: str) -> Path:
    value = config_files.get(key)
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"config_files.{key} must be a non-empty path string")
        return _resolve_repo_path(repo_root, value)
    discovered = _discover_named_file(artifact_root, CONFIG_FILE_NAMES[key])
    return discovered or artifact_root / ".lineage-missing" / CONFIG_FILE_NAMES[key][0]


def _platform_for_layer(layer: str) -> str:
    return f"FABRIC_{layer.upper()}"


def _discover_lakehouse_platforms(artifact_root: Path) -> dict:
    platforms = dict(DEFAULT_LAKEHOUSE_PLATFORM)
    for path in artifact_root.rglob("*.Lakehouse"):
        if not path.is_dir() or _is_ignored(path, artifact_root):
            continue
        parts = {part.casefold() for part in path.relative_to(artifact_root).parts}
        for layer in ("bronze", "silver", "gold"):
            if layer in parts:
                platforms[path.name[:-len(".Lakehouse")].casefold()] = _platform_for_layer(layer)
                break
    return platforms


def discover_repository(repo_root: Path,
                        config_path: Optional[Path] = None) -> RepositoryLayout:
    repo_root = repo_root.expanduser().resolve()
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository folder not found: {repo_root}")

    resolved_profile_path = _profile_path(repo_root, config_path)
    profile = _load_profile(resolved_profile_path)
    artifact_root_value = profile.get("artifact_root", ".")
    if not isinstance(artifact_root_value, str) or not artifact_root_value.strip():
        raise ValueError("artifact_root must be a non-empty path string")
    artifact_root = _resolve_repo_path(repo_root, artifact_root_value)
    if not artifact_root.is_dir():
        raise NotADirectoryError(f"Configured artifact root not found: {artifact_root}")

    layers = profile.get("layers") or {}
    if not isinstance(layers, dict):
        raise ValueError("layers must be a JSON object")
    layer_roots = {}
    warnings = []
    for layer in ("bronze", "silver", "gold"):
        roots = _configured_paths(repo_root, layers.get(layer))
        if not roots:
            roots = _discover_layer_roots(artifact_root, layer)
        missing = [path for path in roots if not path.is_dir()]
        if missing:
            raise NotADirectoryError(
                f"Configured {layer} notebook folder not found: {missing[0]}"
            )
        if not roots:
            warnings.append(f"No {layer} notebook folder discovered")
        layer_roots[layer] = tuple(roots)

    config_files = profile.get("config_files") or {}
    if not isinstance(config_files, dict):
        raise ValueError("config_files must be a JSON object")

    semantic_root = _resolve_repo_path(
        repo_root, profile.get("semantic_models_root", artifact_root)
    )
    reports_root = _resolve_repo_path(repo_root, profile.get("reports_root", artifact_root))
    pipelines_root = _resolve_repo_path(repo_root, profile.get("pipelines_root", artifact_root))
    output_dir = _resolve_repo_path(repo_root, profile.get("output_dir", "lineage-output"))

    platforms = _discover_lakehouse_platforms(artifact_root)
    configured_platforms = profile.get("lakehouse_platforms") or {}
    if not isinstance(configured_platforms, dict):
        raise ValueError("lakehouse_platforms must be a JSON object")
    platforms.update({str(key).casefold(): str(value).upper()
                      for key, value in configured_platforms.items()})

    variables = dict(DEFAULT_LAKEHOUSE_VARS)
    configured_variables = profile.get("lakehouse_variables") or {}
    if not isinstance(configured_variables, dict):
        raise ValueError("lakehouse_variables must be a JSON object")
    variables.update({str(key): str(value) for key, value in configured_variables.items()})

    template = profile.get("lakehouse_files_url_template", "")
    if not isinstance(template, str):
        raise ValueError("lakehouse_files_url_template must be a string")
    if template and "{name}" not in template:
        raise ValueError("lakehouse_files_url_template must contain the {name} placeholder")

    shortcut_metadata = tuple(sorted(
        (path.resolve() for path in artifact_root.rglob("*.Lakehouse/shortcuts.metadata.json")
         if not _is_ignored(path, artifact_root)),
        key=lambda item: item.as_posix().casefold(),
    ))

    return RepositoryLayout(
        repo_root=repo_root,
        artifact_root=artifact_root,
        bronze_notebooks=layer_roots["bronze"],
        silver_notebooks=layer_roots["silver"],
        gold_notebooks=layer_roots["gold"],
        pipelines_root=pipelines_root,
        semantic_models_root=semantic_root,
        reports_root=reports_root,
        sources_config=_configured_or_discovered_file(
            repo_root, artifact_root, config_files, "sources"
        ),
        static_files_config=_configured_or_discovered_file(
            repo_root, artifact_root, config_files, "static_files"
        ),
        redshift_direct_config=_configured_or_discovered_file(
            repo_root, artifact_root, config_files, "redshift_direct"
        ),
        shortcut_metadata=shortcut_metadata,
        output_dir=output_dir,
        profile_path=resolved_profile_path,
        lakehouse_platforms=platforms,
        lakehouse_variables=variables,
        lakehouse_files_url_template=template,
        warnings=tuple(warnings),
    )


def activate_repository(layout: RepositoryLayout) -> None:
    global WORKSPACE, FABRIC_ROOT
    global BRONZE_NOTEBOOKS, SILVER_NOTEBOOKS, GOLD_NOTEBOOKS
    global SEMANTIC_MODELS, REPORTS_ROOT, PIPELINES_ROOT
    global BRONZE_NOTEBOOK_ROOTS, SILVER_NOTEBOOK_ROOTS, GOLD_NOTEBOOK_ROOTS
    global SOURCES_CONFIG, STATIC_FILES_CONFIG, REDSHIFT_DIRECT_CONFIG, SHORTCUT_METADATA
    global LAKEHOUSE_PLATFORM, LAKEHOUSE_VARS, BRONZE_LAKEHOUSE_FILES_URL

    WORKSPACE = layout.repo_root
    FABRIC_ROOT = layout.artifact_root
    BRONZE_NOTEBOOK_ROOTS = list(layout.bronze_notebooks)
    SILVER_NOTEBOOK_ROOTS = list(layout.silver_notebooks)
    GOLD_NOTEBOOK_ROOTS = list(layout.gold_notebooks)
    BRONZE_NOTEBOOKS = (BRONZE_NOTEBOOK_ROOTS[0] if BRONZE_NOTEBOOK_ROOTS
                        else FABRIC_ROOT / ".lineage-missing" / "bronze")
    SILVER_NOTEBOOKS = (SILVER_NOTEBOOK_ROOTS[0] if SILVER_NOTEBOOK_ROOTS
                        else FABRIC_ROOT / ".lineage-missing" / "silver")
    GOLD_NOTEBOOKS = (GOLD_NOTEBOOK_ROOTS[0] if GOLD_NOTEBOOK_ROOTS
                      else FABRIC_ROOT / ".lineage-missing" / "gold")
    SEMANTIC_MODELS = layout.semantic_models_root
    REPORTS_ROOT = layout.reports_root
    PIPELINES_ROOT = layout.pipelines_root
    SOURCES_CONFIG = layout.sources_config
    STATIC_FILES_CONFIG = layout.static_files_config
    REDSHIFT_DIRECT_CONFIG = layout.redshift_direct_config
    SHORTCUT_METADATA = list(layout.shortcut_metadata)
    LAKEHOUSE_PLATFORM = dict(layout.lakehouse_platforms)
    LAKEHOUSE_VARS = dict(layout.lakehouse_variables)
    BRONZE_LAKEHOUSE_FILES_URL = layout.lakehouse_files_url_template

# ─────────────────────────── patterns ───────────────────────────
FABRIC_META_LINE = re.compile(r"^#\s*(META\b|CELL\s+\*+|MARKDOWN\s+\*+|METADATA\s+\*+)")

SAVE_AS_TABLE = re.compile(r"""saveAsTable\s*\(\s*f?["']([^"']+)["']""")
SPARK_TABLE = re.compile(
    r"""(?:spark(?:\.read(?:\.format\s*\([^)]*\))?)?\.table|spark\.catalog\.tableExists|"""
    r"""spark\.catalog\.refreshTable)\s*\(\s*f?["']([^"']+)["']"""
)
READ_FIRST_EXISTING = re.compile(r"_read_first_existing\s*\(\s*\[(.*?)\]", re.DOTALL)
STRING_LITERAL = re.compile(r"""["']([^"'\n]+)["']""")
TRIPLE_QUOTED = re.compile(r'("""|\'\'\')(.*?)\1', re.DOTALL)
SPARK_SQL_INLINE = re.compile(r"""spark\.sql\s*\(\s*f?["']([^"'\n]+)["']""")
SQL_TABLE_REF = re.compile(
    # sqlglot wraps join trees in parens, so allow `FROM (tbl AS a JOIN ...)` while
    # still skipping derived tables like `FROM (SELECT ...)`
    r"""\b(?:FROM|JOIN)\s+(?:\(\s*)*(?!SELECT\b|WITH\b|VALUES\b|LATERAL\b)"""
    r"""((?:\[[^\]]+\]|`[^`]+`|"[^"]+"|[A-Za-z_]\w*)"""
    r"""(?:\s*\.\s*(?:\[[^\]]+\]|`[^`]+`|"[^"]+"|\w+)){1,2})""",
    re.IGNORECASE,
)
TEMP_VIEW = re.compile(r"""(?:createOrReplaceTempView|createTempView|registerTempTable)\s*\(\s*f?["']([^"']+)["']""")
HELPER_DEF = re.compile(r"^def\s+(\w+)\s*\(([^)]*)\)\s*:\n((?:(?:[ \t]+.*)?\n)+)", re.MULTILINE)
HELPER_FSTRING = re.compile(r"""f["']([^"'\n]*\{[^"'\n]*)["']""")
CTE_NAME = re.compile(r"""(?:WITH|,)\s+([A-Za-z_]\w*)\s+AS\s*\(""", re.IGNORECASE)
TRIPLE_QUOTED_ASSIGNMENT = re.compile(
    r'''^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(?:[rubfRUBF]*)?("""|\'\'\')(.*?)\2''',
    re.MULTILINE | re.DOTALL,
)
DERIVED_QUERIES_BLOCK = re.compile(
    r"^\s*DERIVED_QUERIES\s*=\s*\{(.*?)^\s*\}", re.MULTILINE | re.DOTALL,
)
DERIVED_QUERY_ENTRY = re.compile(r'''["']([^"']+)["']\s*:\s*([A-Z_][A-Z0-9_]*)''')

ASSIGN_LITERAL = re.compile(r"""^\s*([A-Z_][A-Z0-9_]*)\s*=\s*["']([^"']+)["']\s*$""", re.MULTILINE)
BUILD_TABLE_PATH = re.compile(r"""build_table_path\s*\(\s*([^,)]+?)\s*(?:,\s*([^)]+?)\s*)?\)""")

# TMDL
TMDL_TABLE_NAME = re.compile(r"^\s*table\s+(.+?)\s*$", re.MULTILINE)
TMDL_ENTITY_NAME = re.compile(r"^\s*entityName:\s*(.+?)\s*$", re.MULTILINE)
TMDL_SCHEMA_NAME = re.compile(r"^\s*schemaName:\s*(.+?)\s*$", re.MULTILINE)
TMDL_EXPRESSION_SOURCE = re.compile(r"^\s*expressionSource:\s*(.+?)\s*$", re.MULTILINE)
TMDL_M_ITEM = re.compile(r"""\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]""")

AUTO_DATE_TABLE = re.compile(r"^(LocalDateTable_|DateTableTemplate_)")

SQL_NOISE = {
    "select", "where", "group", "order", "on", "as", "and", "or", "lateral",
    "unnest", "values", "dual", "left", "right", "inner", "outer", "full", "cross",
}


# ─────────────────────────── helpers ───────────────────────────
def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def strip_fabric_metadata(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not FABRIC_META_LINE.match(ln))


def unquote(part: str) -> str:
    return part.strip().strip("`\"[]").strip()


def parse_ref(raw: str):
    """Normalise a table reference into (lakehouse, schema, object) or None."""
    ref = raw.strip()
    for var, name in LAKEHOUSE_VARS.items():
        ref = ref.replace(var, name)
    if "{" in ref or "%" in ref:
        return None
    parts = [unquote(p) for p in ref.split(".")]
    if len(parts) == 2:
        lakehouse, schema, obj = "", parts[0], parts[1]
    elif len(parts) == 3:
        lakehouse, schema, obj = parts[0], parts[1], parts[2]
    else:
        return None
    if not schema or not obj:
        return None
    # Delta table names may start with a digit (e.g. sales.2024_partial_year_reps), but a part
    # made only of digits is a numeric literal, not an identifier.
    if not all(re.fullmatch(r"\w+", part) and re.search(r"[A-Za-z_]", part)
               for part in (schema, obj)):
        return None
    if schema.lower() in SQL_NOISE or obj.lower() in SQL_NOISE:
        return None
    return lakehouse.lower(), schema.lower(), obj.lower()


class PlatformResolver:
    """Resolves the Fabric layer of a table reference."""

    def __init__(self):
        self.silver_schemas: set[str] = set()
        self.bronze_schemas: set[str] = set()
        self.gold_schemas: set[str] = {"gold"}

    @property
    def known_schemas(self) -> set[str]:
        return self.silver_schemas | self.bronze_schemas | self.gold_schemas

    def observe(self, ref):
        lakehouse, schema, _ = ref
        platform = LAKEHOUSE_PLATFORM.get(lakehouse)
        if platform == "FABRIC_BRONZE":
            self.bronze_schemas.add(schema)
        elif platform == "FABRIC_SILVER":
            self.silver_schemas.add(schema)
        elif platform == "FABRIC_GOLD":
            self.gold_schemas.add(schema)

    def platform(self, ref) -> str:
        lakehouse, schema, _ = ref
        explicit = LAKEHOUSE_PLATFORM.get(lakehouse)
        if explicit:
            return explicit
        if schema in self.gold_schemas:
            return "FABRIC_GOLD"
        if schema in self.silver_schemas:
            return "FABRIC_SILVER"
        return "FABRIC_BRONZE"

    def is_ambiguous(self, ref) -> str:
        lakehouse, schema, _ = ref
        if lakehouse in LAKEHOUSE_PLATFORM:
            return "No"
        overlap = sum(
            schema in bucket
            for bucket in (self.silver_schemas, self.bronze_schemas, self.gold_schemas)
        )
        return "Yes" if overlap > 1 else "No"


# ─────────────────────────── lakehouse shortcuts ───────────────────────────
def _dotnet_guid_bytes(guid: str) -> bytes:
    raw = bytes.fromhex(guid.replace("-", ""))
    return raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]


def _guid_from_bytes(data: bytes) -> str:
    hexed = (data[3::-1] + data[5:3:-1] + data[7:5:-1] + data[8:]).hex()
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


def shortcut_item_id(lakehouse_guid: str) -> str:
    """shortcuts.metadata.json stores itemIds as the byte-reversed form of the lakehouse GUID."""
    return _guid_from_bytes(bytes(reversed(_dotnet_guid_bytes(lakehouse_guid))))


NOTEBOOK_LAKEHOUSE = re.compile(
    r'"default_lakehouse":\s*"([0-9a-fA-F-]{36})".*?"default_lakehouse_name":\s*"([^"]+)"',
    re.DOTALL,
)


def lakehouse_names_by_item_id() -> dict:
    """Maps both the plain and byte-reversed lakehouse GUID to its lakehouse name."""
    mapping = {}
    for path in FABRIC_ROOT.rglob("notebook-content.py"):
        text = read_text(path).replace("# META ", "")
        for guid, name in NOTEBOOK_LAKEHOUSE.findall(text):
            mapping[guid.lower()] = name.lower()
            mapping[shortcut_item_id(guid).lower()] = name.lower()
    return mapping


def load_shortcuts(stats: Counter) -> list:
    """Shortcut definitions as (shortcut_ref, target_ref, metadata path).

    The shortcut is kept as its own node so lineage reads
    Bronze/Silver source -> gold_lh shortcut -> consumer.
    """
    names = lakehouse_names_by_item_id()
    shortcuts = []
    for meta in SHORTCUT_METADATA:
        try:
            entries = json.loads(read_text(meta))
        except json.JSONDecodeError:
            stats["shortcut_files_unreadable"] += 1
            continue
        stats["shortcut_files"] += 1
        host = meta.parent.name[: -len(".Lakehouse")].lower()
        for entry in entries:
            one_lake = (entry.get("target") or {}).get("oneLake") or {}
            target = (one_lake.get("path") or "").strip("/").split("/")
            if len(target) != 3 or target[0].lower() != "tables":
                stats["shortcuts_unresolved"] += 1
                continue
            lakehouse = names.get((one_lake.get("itemId") or "").lower(), "")
            shortcut_ref = (host,
                            entry.get("path", "").strip("/").split("/")[-1].lower(),
                            entry.get("name", "").lower())
            shortcuts.append((shortcut_ref, (lakehouse, target[1].lower(), target[2].lower()),
                              rel_path(meta)))
            stats["shortcuts"] += 1
            if not lakehouse:
                stats["shortcuts_unknown_lakehouse"] += 1
    return shortcuts


def shortcut_rows(shortcuts: list, resolver: PlatformResolver) -> list:
    """One edge per shortcut: the physical table feeds the shortcut that exposes it."""
    rows = []
    for shortcut_ref, target_ref, meta_path in shortcuts:
        _, schema, obj = shortcut_ref
        rows.append(make_row(
            schema, obj, resolver.platform(shortcut_ref), "SHORTCUT",
            target_ref, resolver, "SHORTCUT", f"{schema}.{obj}", meta_path,
        ))
        rows[-1]["Dependency_Type"] = "SHORTCUT"
    return rows


def make_row(out_schema, out_object, out_platform, out_type,
             ref, resolver: PlatformResolver,
             tool, transform_name, transform_path) -> dict:
    """Flows left-to-right: `ref` (upstream read) is the Source, the produced artifact the Target."""
    _, src_schema, src_object = ref
    return {
        "Source_Schema": src_schema,
        "Source_Object": src_object,
        "Source_Type": "TABLE",
        "Target_Schema": out_schema,
        "Target_Object": out_object,
        "Target_Type": out_type,
        "Dependency_Type": "DATA_FLOW",
        "Column_Or_Parameter": "*",
        "Schema_Bound": "No",
        "External_Location": "NULL",
        "Is_Ambiguous": resolver.is_ambiguous(ref),
        "Location_Type": "LOCAL",
        "Source_Platform": resolver.platform(ref),
        "Target_Platform": out_platform,
        "Transform_Tool": tool,
        "Transform_Name": transform_name,
        "Transform_Path": transform_path,
        "Source_Column": "NULL",
    }


def rel_path(path: Path) -> str:
    return path.relative_to(WORKSPACE).as_posix()


# ─────────────────────────── notebook extraction ───────────────────────────
def explicit_refs(code: str):
    """References we trust unconditionally (API calls with a qualified name)."""
    found = []
    for pattern in (SPARK_TABLE, SAVE_AS_TABLE):
        for raw in pattern.findall(code):
            ref = parse_ref(raw)
            if ref:
                found.append(ref)
    for block in READ_FIRST_EXISTING.findall(code):
        for raw in STRING_LITERAL.findall(block):
            ref = parse_ref(raw)
            if ref:
                found.append(ref)
    found.extend(helper_read_refs(code))
    return found


def sql_blocks(code: str):
    blocks = [body for _, body in TRIPLE_QUOTED.findall(code)]
    blocks.extend(SPARK_SQL_INLINE.findall(code))
    return blocks


def helper_read_refs(code: str):
    """Table names passed to locally-defined helpers that wrap spark.table/spark.read.

    Handles helpers that qualify the name themselves, e.g. `f"Bronze_LH.lake.{name}"`.
    """
    refs = []
    for name, params, body in HELPER_DEF.findall(code):
        if not re.search(r"spark\.(?:table|read|catalog)\b", body):
            continue
        first_param = params.split(",")[0].split("=")[0].split(":")[0].strip()
        templates = {"\0"}
        if first_param:
            for literal in HELPER_FSTRING.findall(body):
                template = literal.replace("{" + first_param + "}", "\0")
                # skip templates that still interpolate something we cannot resolve
                if "\0" in template and "{" not in template:
                    templates.add(template)
        call = re.compile(rf"""\b{re.escape(name)}\s*\(\s*f?["']([^"']+)["']""")
        for raw in call.findall(code):
            for template in templates:
                ref = parse_ref(template.replace("\0", raw))
                if ref:
                    refs.append(ref)
        # helpers that take a candidate list, e.g. read_source(["Bronze_LH.lake.x", "lake.x"])
        call_list = re.compile(rf"\b{re.escape(name)}\s*\(\s*\[(.*?)\]", re.DOTALL)
        for block in call_list.findall(code):
            for raw in STRING_LITERAL.findall(block):
                ref = parse_ref(raw)
                if ref:
                    refs.append(ref)
    return refs


def notebook_reads(code: str, resolver: PlatformResolver):
    """Ordered, de-duplicated upstream references for one notebook."""
    local_names = {n.lower() for n in TEMP_VIEW.findall(code)}
    reads: dict[tuple, tuple] = {}

    def add(ref):
        if ref[2] in local_names:
            return
        key = (ref[1], ref[2])
        # a lakehouse-qualified reference supersedes the bare one for the same table
        if key not in reads or (ref[0] and not reads[key][0]):
            reads[key] = ref

    for raw in SPARK_TABLE.findall(code):
        ref = parse_ref(raw)
        if ref:
            add(ref)
    for ref in helper_read_refs(code):
        add(ref)
    for block in READ_FIRST_EXISTING.findall(code):
        for raw in STRING_LITERAL.findall(block):
            ref = parse_ref(raw)
            if ref:
                add(ref)
    for block in sql_blocks(code):
        cte_names = {n.lower() for n in CTE_NAME.findall(block)}
        for raw in SQL_TABLE_REF.findall(block):
            ref = parse_ref(raw)
            # loose SQL matches must land in a schema we have seen elsewhere
            if ref and ref[1] in resolver.known_schemas and ref[2] not in cte_names:
                add(ref)
    return list(reads.values())


def notebook_files(roots):
    if isinstance(roots, Path):
        roots = [roots]
    files = {
        path.resolve()
        for root in roots
        if root.exists()
        for path in root.rglob("notebook-content.py")
        if not _is_ignored(path, root)
    }
    return sorted(files, key=lambda item: item.as_posix().casefold())


TABLE_SECTION = re.compile(r"(?m)^# ## Table:\s+.+$")


def table_sections(code: str):
    """Yield table-heading sections so multi-table notebooks keep sources local."""
    headings = list(TABLE_SECTION.finditer(code))
    if not headings:
        return [(None, code)]
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(code)
        sections.append((heading.group(0), code[heading.start():end]))
    return sections


def silver_targets(code: str, path: Path):
    targets = []
    for raw in SAVE_AS_TABLE.findall(code):
        ref = parse_ref(raw)
        if ref:
            targets.append((ref[1], ref[2]))
    if targets:
        return targets
    # fall back to folder/notebook naming: <schema>/Refresh_<schema>_<object>.Notebook
    schema = path.parent.parent.name.lower()
    stem = path.parent.name[: -len(".Notebook")]
    match = re.fullmatch(rf"Refresh_{re.escape(schema)}_(.+)", stem, re.IGNORECASE)
    if match:
        return [(schema, match.group(1).lower())]
    return []


def gold_targets(code: str):
    constants = {name: value for name, value in ASSIGN_LITERAL.findall(code)}
    targets = []
    for raw in SAVE_AS_TABLE.findall(code):
        ref = parse_ref(raw)
        if ref:
            targets.append((ref[1], ref[2]))
    for name_arg, layer_arg in BUILD_TABLE_PATH.findall(code):
        if name_arg.startswith("def "):
            continue
        obj = constants.get(name_arg.strip()) or _literal(name_arg)
        layer = constants.get((layer_arg or "").strip()) or _literal(layer_arg) or "gold"
        if obj:
            targets.append((layer.lower(), obj.lower()))
    # preserve order, drop duplicates
    return list(dict.fromkeys(targets))


def _literal(token: str):
    token = (token or "").strip()
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
    return None


def parse_notebooks(root: Path, layer: str, resolver: PlatformResolver, stats: Counter):
    rows = []
    for path in notebook_files(root):
        code = strip_fabric_metadata(read_text(path))
        stats[f"{layer}_notebooks"] += 1
        platform = _platform_for_layer(layer)
        sections = table_sections(code) if layer == "silver" else [(None, code)]
        for _, section in sections:
            targets = silver_targets(section, path) if layer == "silver" else gold_targets(section)
            if not targets:
                stats[f"{layer}_skipped_no_output"] += 1
                continue
            reads = notebook_reads(section, resolver)
            if not reads:
                stats[f"{layer}_no_sources"] += 1
                continue
            for schema, obj in targets:
                for ref in reads:
                    if ((ref[1], ref[2]) == (schema, obj)
                            and resolver.platform(ref) == platform):
                        continue
                    rows.append(make_row(
                        schema, obj, platform, "TABLE", ref, resolver,
                        "PYTHON", f"{schema}.{obj}", rel_path(path),
                    ))
    return rows


def seed_resolver(resolver: PlatformResolver, roots):
    """First pass: learn which schemas belong to which lakehouse."""
    for root in roots:
        for path in notebook_files(root):
            code = strip_fabric_metadata(read_text(path))
            for ref in explicit_refs(code):
                resolver.observe(ref)
    for path in notebook_files(SILVER_NOTEBOOK_ROOTS):
        code = strip_fabric_metadata(read_text(path))
        for raw in SAVE_AS_TABLE.findall(code):
            ref = parse_ref(raw)
            if ref:
                resolver.silver_schemas.add(ref[1])
    for root in SILVER_NOTEBOOK_ROOTS:
        for child in root.iterdir():
            if child.is_dir() and not child.name.endswith(".Notebook"):
                resolver.silver_schemas.add(child.name.lower())


# ─────────────────────────── bronze ingestion ───────────────────────────
EXTERNAL_PLATFORM = {
    "salesforce": "SALESFORCE", "s3": "S3", "azure": "AZURE", "ga4": "GOOGLE_ANALYTICS",
    "gainsight": "GAINSIGHT", "gong": "GONG", "outreach": "OUTREACH", "conductor": "CONDUCTOR",
    "linkedin": "LINKEDIN", "eloqua": "ELOQUA", "eloqua_bulk": "ELOQUA",
    "legisway": "LEGISWAY", "customer_gauge": "CUSTOMER_GAUGE",
    "oracle": "ORACLE", "sqlserver": "SQL_SERVER", "athena": "ATHENA",
}
SOURCE_NAME_PLATFORM = {"IC_ADMIN": "ICADMIN", "IC_ADMIN_UNIQUE_ENDUSER": "ICADMIN",
                        "TAGETIK": "TAGETIK", "SAP_PWK0": "SAP"}
BRONZE_SOURCE_NAME = re.compile(r"""BRONZE_SOURCE_NAME\s*=\s*["']([A-Z0-9_]+)["']""")
BRONZE_ENRICH_NAME = re.compile(r"""BRONZE_ENRICH_NAME\s*=\s*["']([A-Z0-9_]+)["']""")
ENRICH_DEFAULT_TARGETS_BLOCK = re.compile(
    r"^\s*ENRICH_DEFAULT_TARGETS\s*=\s*\[(.*?)\]", re.MULTILINE | re.DOTALL,
)
LAKE_TABLE_LITERAL = re.compile(r'''lake_table\s*\(\s*["']([^"']+)["']\s*\)''')
STATIC_INGEST_SCHEMA = re.compile(r"""run_static_ingestion\(\s*["']([a-z0-9_]+)["']""")

# The ingest configs are read from the Bronze lakehouse Files area, not from the repo copy.
BRONZE_LAKEHOUSE_FILES_URL = (
    "https://app.powerbi.com/groups/9dfa32d5-1e73-48e8-b821-1e4912e4565c"
    "/lakehouses/552a027b-1713-4af9-a0e2-f80817c7c340"
    "?trident=1&experience=power-bi&ctid=8ac76c91-e7f1-41ff-a89c-3553b2da2c17"
    "&selectedPath=Files%2F{name}&extensionScenario=openArtifact"
)


def lakehouse_file_url(files_path: str) -> str:
    """`files_path` is relative to the lakehouse Files/ root, e.g. 'Static/x.json'."""
    if not BRONZE_LAKEHOUSE_FILES_URL:
        return "NULL"
    return BRONZE_LAKEHOUSE_FILES_URL.format(name=files_path.replace("/", "%2F"))


def fabric_notebook(name: str) -> str:
    """Find a named Bronze notebook for Transform_Path evidence."""
    for root in BRONZE_NOTEBOOK_ROOTS:
        path = root / f"{name}.Notebook" / "notebook-content.py"
        if path.exists():
            return rel_path(path)
    return rel_path(BRONZE_NOTEBOOKS)


def ingest_notebooks(marker: re.Pattern) -> dict:
    """Maps a marker value to the notebook that declares it.

    Bronze/Notebooks wins over the root-level one-off notebooks (e.g. the *_REPAIR variants).
    """
    notebooks = {}
    canonical = notebook_files(BRONZE_NOTEBOOK_ROOTS)
    for path in canonical + [p for p in notebook_files(FABRIC_ROOT) if p not in set(canonical)]:
        for value in marker.findall(strip_fabric_metadata(read_text(path))):
            notebooks.setdefault(value, rel_path(path))
    return notebooks


def bronze_source_notebooks() -> dict:
    return ingest_notebooks(BRONZE_SOURCE_NAME)


def bronze_enrichment_notebooks() -> dict:
    """Maps explicitly listed enrichment targets to the notebook that rewrites them."""
    notebooks = {}
    for path in notebook_files(BRONZE_NOTEBOOK_ROOTS):
        code = strip_fabric_metadata(read_text(path))
        if not BRONZE_ENRICH_NAME.search(code):
            continue
        targets_match = ENRICH_DEFAULT_TARGETS_BLOCK.search(code)
        if not targets_match:
            continue
        for target in STRING_LITERAL.findall(targets_match.group(1)):
            notebooks[target.lower()] = rel_path(path)
    return notebooks


def external_object(table: dict) -> str:
    """Best identifier for the thing being read out of the external system."""
    for key in ("source_object", "sobject", "object_key", "activity_type", "endpoint", "target"):
        value = table.get(key)
        if value:
            return str(value).strip().strip("/").lower()
    return ""


def physical_sql_refs(sql: str) -> list:
    """Ordered physical FROM/JOIN references, excluding CTE names and duplicates."""
    refs = []
    seen = set()
    cte_names = {name.lower() for name in CTE_NAME.findall(sql)}
    for raw in SQL_TABLE_REF.findall(sql):
        ref = parse_ref(raw)
        if not ref or ref[1] in cte_names or (ref[1], ref[2]) in seen:
            continue
        seen.add((ref[1], ref[2]))
        refs.append(ref)
    return refs


def parse_bronze(config_path: Path, resolver: PlatformResolver, stats: Counter) -> list:
    if not config_path.exists():
        return []
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    schema = ((config.get("lakehouse") or {}).get("schema") or "lake").lower()
    notebooks = bronze_source_notebooks()
    enrichment_notebooks = bronze_enrichment_notebooks()
    fallback = fabric_notebook("Ingestion_Core")
    rows = []

    for source_name, source in (config.get("sources") or {}).items():
        stats["bronze_sources"] += 1
        kind = (source.get("kind") or "").lower()
        protocol = (source.get("api_type") or source.get("db_type") or "").lower()
        platform = (SOURCE_NAME_PLATFORM.get(source_name)
                    or EXTERNAL_PLATFORM.get(protocol)
                    or ("API" if kind == "api" else "FILE"))
        source_type = "API_ENDPOINT" if kind == "api" else "TABLE"
        source_transform_path = notebooks.get(source_name, fallback)

        tables = source.get("tables") or []
        if isinstance(tables, dict):
            tables = list(tables.values())
        for table in tables:
            target = str(table.get("target") or "").strip().lower()
            obj = external_object(table)
            if not target or not obj:
                stats["bronze_tables_skipped"] += 1
                continue
            stats["bronze_tables"] += 1
            transform_path = enrichment_notebooks.get(target, source_transform_path)
            refs = physical_sql_refs(str(table.get("athena_query") or ""))
            if not refs:
                refs = [("", source_name.lower(), obj)]
            elif len(refs) > 1:
                stats["bronze_athena_query_sources"] += len(refs)

            for ref in refs:
                rows.append(make_row(
                    schema, target, "FABRIC_BRONZE", "TABLE", ref, resolver,
                    "PYTHON", f"{source_name.lower()}.{target}", transform_path,
                ))
                rows[-1].update({
                    "Source_Type": source_type,
                    "Source_Platform": platform,
                    "Location_Type": "REMOTE",
                    "External_Location": lakehouse_file_url(config_path.name),
                })
    return rows


def parse_bronze_enrichment_lookups(config_path: Path, resolver: PlatformResolver,
                                    stats: Counter) -> list:
    """Local Bronze lookups conditionally joined into explicitly enriched targets."""
    if not config_path.exists():
        return []
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    table_specs = {
        str(table.get("target") or "").lower(): table
        for source in (config.get("sources") or {}).values()
        for table in ((source.get("tables") or {}).values()
                      if isinstance(source.get("tables"), dict)
                      else (source.get("tables") or []))
    }
    column_lookups = {
        "carbonprodname": "qs_mediregs_products",
        "search_name": "qs_mediregs_client_event_search_name_map",
        "account_name": "qs_accounts",
    }
    segment_columns = {
        "sold_to_market_segment", "sold_to_market_sub_segment", "lmg_service_level",
    }
    rows = []
    for path in notebook_files(BRONZE_NOTEBOOK_ROOTS):
        code = strip_fabric_metadata(read_text(path))
        if not BRONZE_ENRICH_NAME.search(code):
            continue
        targets_match = ENRICH_DEFAULT_TARGETS_BLOCK.search(code)
        if not targets_match:
            continue
        available_lookups = {name.lower() for name in LAKE_TABLE_LITERAL.findall(code)}
        for target in (value.lower() for value in STRING_LITERAL.findall(targets_match.group(1))):
            columns = {str(column).lower() for column in table_specs.get(target, {}).get("columns", [])}
            lookups = {
                table for column, table in column_lookups.items() if column in columns
            }
            if columns & segment_columns:
                lookups.update({
                    "qs_market_segmentation_service_level", "qs_market_segmentation",
                })
            for lookup in sorted(lookups & available_lookups):
                row = make_row(
                    "lake", target, "FABRIC_BRONZE", "TABLE",
                    ("", "lake", lookup), resolver, "PYTHON",
                    f"enriched.{target}", rel_path(path),
                )
                row["Source_Platform"] = "FABRIC_BRONZE"
                rows.append(row)
                stats["bronze_enrichment_lookup_sources"] += 1
    return rows


def parse_bronze_derived_queries(root: Path, resolver: PlatformResolver,
                                 stats: Counter) -> list:
    """Athena sources for Bronze tables defined by SQL inside ingestion notebooks."""
    rows = []
    for path in notebook_files(root):
        code = strip_fabric_metadata(read_text(path))
        mapping_match = DERIVED_QUERIES_BLOCK.search(code)
        if not mapping_match:
            continue

        sql_by_variable = {
            variable: sql
            for variable, _, sql in TRIPLE_QUOTED_ASSIGNMENT.findall(code)
        }
        for target, variable in DERIVED_QUERY_ENTRY.findall(mapping_match.group(1)):
            sql = sql_by_variable.get(variable)
            if not sql:
                stats["bronze_derived_queries_skipped"] += 1
                continue

            for ref in physical_sql_refs(sql):
                row = make_row(
                    "lake", target.lower(), "FABRIC_BRONZE", "TABLE",
                    ref, resolver, "PYTHON", f"derived.{target.lower()}", rel_path(path),
                )
                row.update({
                    "Source_Platform": "ATHENA",
                    "Location_Type": "REMOTE",
                    "External_Location": lakehouse_file_url(SOURCES_CONFIG.name),
                })
                rows.append(row)
                stats["bronze_derived_query_sources"] += 1
            stats["bronze_derived_queries"] += 1
    return rows


def static_ingest_notebooks() -> dict:
    return ingest_notebooks(STATIC_INGEST_SCHEMA)


def normalized_file_stem(file_name: str) -> str:
    """Mirrors normalized_file_stem() in Ingest_Static_Core."""
    stem = re.sub(r"\.[^.]*$", "", file_name.rsplit("/", 1)[-1])
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", stem.strip()).strip("_").lower()
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        return ""
    return f"file_{normalized}" if normalized[0].isdigit() else normalized


def static_target_table(entry: dict) -> str:
    """`normalized_file_stem` entries derive the table name from the file name."""
    if str(entry.get("target_name_strategy") or "").strip().lower() == "normalized_file_stem":
        return normalized_file_stem(str(entry.get("file_name") or ""))
    table = str(entry.get("target_table") or "").strip().lower()
    return table if re.fullmatch(r"[a-z_]\w*", table) else ""


def parse_static_files(config_path: Path, resolver: PlatformResolver, stats: Counter) -> list:
    """Static CSV/Excel files landed in the lakehouse Files/Static area."""
    if not config_path.exists():
        return []
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    notebooks = static_ingest_notebooks()
    fallback = fabric_notebook("Ingest_Static_Core")
    config_url = lakehouse_file_url(config_path.name)
    rows = []

    for entry in config.get("files") or []:
        schema = str(entry.get("target_schema") or "").strip().lower()
        table = static_target_table(entry)
        file_name = str(entry.get("file_name") or "").strip()
        if not entry.get("enabled") or not schema or not table or not file_name:
            stats["static_files_skipped"] += 1
            continue
        stats["static_files"] += 1
        rows.append(make_row(
            schema, table, "FABRIC_BRONZE", "TABLE",
            ("", "static_files", file_name.lower()), resolver,
            "PYTHON", f"{schema}.{table}", notebooks.get(schema, fallback),
        ))
        rows[-1].update({
            "Source_Type": (entry.get("file_type") or "file").upper(),
            "Source_Platform": "FILE",
            "Location_Type": "REMOTE",
            "External_Location": config_url,
        })
    return rows


def parse_redshift_direct(config_path: Path, resolver: PlatformResolver, stats: Counter) -> list:
    """Redshift tables copied straight into Bronze by Ingest_Redshift_Direct_Tables."""
    if not config_path.exists():
        return []
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    notebook = FABRIC_ROOT / "Ingest_Redshift_Direct_Tables.Notebook" / "notebook-content.py"
    transform_path = rel_path(notebook) if notebook.exists() else rel_path(FABRIC_ROOT)
    config_url = lakehouse_file_url("Static/Redshift_direct_tables.JSON")
    rows = []

    for entry in config.get("tables") or []:
        schema = str(entry.get("schema") or "").strip().lower()
        table = str(entry.get("table") or "").strip().lower()
        if not entry.get("enabled", True) or not schema or not table:
            stats["redshift_direct_skipped"] += 1
            continue
        stats["redshift_direct_tables"] += 1
        rows.append(make_row(
            schema, table, "FABRIC_BRONZE", "TABLE",
            ("", "redshift", f"{schema}.{table}"), resolver,
            "PYTHON", f"redshift.{schema}.{table}", transform_path,
        ))
        rows[-1].update({
            "Source_Type": "TABLE",
            "Source_Platform": "REDSHIFT",
            "Location_Type": "REMOTE",
            "External_Location": config_url,
        })
    return rows


# ─────────────────────────── semantic models ───────────────────────────
def semantic_model_platform(expression_source: str, resolver: PlatformResolver, schema: str) -> str:
    token = (expression_source or "").strip().strip("'\"").lower()
    for lakehouse, platform in LAKEHOUSE_PLATFORM.items():
        if lakehouse in token:
            return platform
    return resolver.platform(("", schema, ""))


def parse_semantic_models(root: Path, resolver: PlatformResolver, stats: Counter):
    rows = []
    if not root.exists():
        return rows
    for model_dir in sorted(root.rglob("*.SemanticModel")):
        model_name = model_dir.name[: -len(".SemanticModel")]
        stats["semantic_models"] += 1
        for tmdl in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
            text = read_text(tmdl)
            table_match = TMDL_TABLE_NAME.search(text)
            if not table_match:
                continue
            table_name = unquote(table_match.group(1))
            if AUTO_DATE_TABLE.match(table_name):
                continue
            stats["semantic_model_tables"] += 1
            source_match = TMDL_EXPRESSION_SOURCE.search(text)
            expression_source = source_match.group(1) if source_match else ""

            refs = []
            entity = TMDL_ENTITY_NAME.search(text)
            schema = TMDL_SCHEMA_NAME.search(text)
            if entity:
                refs.append(("", unquote(schema.group(1)).lower() if schema else "gold",
                             unquote(entity.group(1)).lower()))
            for m_schema, m_item in TMDL_M_ITEM.findall(text):
                refs.append(("", m_schema.lower(), m_item.lower()))

            refs = list(dict.fromkeys(refs))
            if not refs:
                stats["semantic_model_tables_no_source"] += 1
                continue
            for ref in refs:
                row = make_row(
                    model_name, table_name, "POWER_BI_TABLE", "SEMANTIC_MODEL_TABLE",
                    ref, resolver, "SEMANTIC_MODEL",
                    f"{model_name}.{table_name}", rel_path(tmdl),
                )
                # a shortcut-resolved ref already knows its lakehouse; otherwise fall back to DirectLake
                if not ref[0]:
                    row["Source_Platform"] = semantic_model_platform(
                        expression_source, resolver, ref[1]
                    )
                rows.append(row)
            rows.append(model_membership_row(model_name, table_name, rel_path(tmdl)))
    return rows


def model_membership_row(model_name: str, table_name: str, transform_path: str) -> dict:
    """Edge from a semantic-model table to the model it belongs to."""
    return {
        "Source_Schema": model_name,
        "Source_Object": table_name,
        "Source_Type": "SEMANTIC_MODEL_TABLE",
        "Target_Schema": "SemanticModel",
        "Target_Object": model_name,
        "Target_Type": "SEMANTIC_MODEL",
        "Dependency_Type": "DATA_FLOW",
        "Column_Or_Parameter": "*",
        "Schema_Bound": "No",
        "External_Location": "NULL",
        "Is_Ambiguous": "No",
        "Location_Type": "LOCAL",
        "Source_Platform": "POWER_BI_TABLE",
        "Target_Platform": "POWER_BI_MODEL",
        "Transform_Tool": "SEMANTIC_MODEL",
        "Transform_Name": f"{model_name}.{table_name}",
        "Transform_Path": transform_path,
        "Source_Column": "NULL",
    }


# ─────────────────────────── power bi reports ───────────────────────────
def report_display_name(report_dir: Path) -> str:
    platform = report_dir / ".platform"
    if platform.exists():
        try:
            name = (json.loads(read_text(platform)).get("metadata") or {}).get("displayName")
            if name:
                return str(name).strip()
        except json.JSONDecodeError:
            pass
    return report_dir.name[: -len(".Report")]


def parse_reports(root: Path, known_models: set, resolver: PlatformResolver, stats: Counter) -> list:
    """Power BI reports attached to a semantic model via definition.pbir."""
    rows = []
    for pbir in sorted(root.rglob("*.Report/definition.pbir")):
        report_dir = pbir.parent
        try:
            reference = (json.loads(read_text(pbir)).get("datasetReference") or {})
        except json.JSONDecodeError:
            stats["reports_unreadable"] += 1
            continue
        by_path = (reference.get("byPath") or {}).get("path")
        if not by_path:
            stats["reports_no_model_path"] += 1
            continue
        model_dir = (report_dir / by_path).resolve()
        model_name = model_dir.name[: -len(".SemanticModel")]
        if model_name not in known_models:
            stats["reports_model_out_of_scope"] += 1
            continue

        stats["reports"] += 1
        name = report_display_name(report_dir)
        rows.append(make_row(
            "Report", name, "POWER_BI_REPORT", "REPORT",
            ("", "SemanticModel", model_name), resolver,
            "POWER_BI_REPORT", f"Report.{name}", rel_path(pbir),
        ))
        rows[-1]["Source_Type"] = "SEMANTIC_MODEL"
        rows[-1]["Source_Platform"] = "POWER_BI_MODEL"
    return rows


# ─────────────────────────── fabric data pipelines ───────────────────────────
def _pipeline_item_catalog(root: Path, item_type: str) -> dict[str, dict[str, str]]:
    """Map Fabric logical IDs to exported artifact identities."""
    catalog: dict[str, dict[str, str]] = {}
    for platform_file in sorted(root.rglob(".platform")):
        if _is_ignored(platform_file, root):
            continue
        try:
            definition = json.loads(read_text(platform_file))
        except (json.JSONDecodeError, OSError):
            continue
        metadata = definition.get("metadata") or {}
        if str(metadata.get("type") or "").casefold() != item_type.casefold():
            continue
        logical_id = str((definition.get("config") or {}).get("logicalId") or "").strip()
        if not logical_id:
            continue
        name = str(metadata.get("displayName") or "").strip()
        if not name:
            name = platform_file.parent.name
            suffix = f".{item_type}"
            if name.casefold().endswith(suffix.casefold()):
                name = name[:-len(suffix)]
            name = html.unescape(name).strip()
        catalog[logical_id.casefold()] = {
            "name": name,
            "path": rel_path(platform_file.parent),
        }
    return catalog


def _pipeline_display_name(pipeline_dir: Path) -> str:
    platform_file = pipeline_dir / ".platform"
    if platform_file.is_file():
        try:
            name = str(
                (json.loads(read_text(platform_file)).get("metadata") or {}).get("displayName")
                or ""
            ).strip()
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass
    name = pipeline_dir.name
    if name.casefold().endswith(".datapipeline"):
        name = name[:-len(".DataPipeline")]
    return html.unescape(name).strip()


def _pipeline_value(value) -> tuple[str, bool]:
    if isinstance(value, dict):
        text = str(value.get("value") or "").strip()
        value_type = str(value.get("type") or "").casefold()
        return text, value_type == "expression" or text.startswith("@")
    text = str(value or "").strip()
    return text, text.startswith("@")


def _pipeline_location_path(location: dict) -> tuple[str, bool]:
    parts = []
    ambiguous = False
    for key in ("folderPath", "fileName"):
        text, dynamic = _pipeline_value(location.get(key))
        if text:
            parts.append(text.strip("/\\"))
        ambiguous = ambiguous or dynamic
    return "/".join(parts), ambiguous


def _pipeline_connection(dataset: dict) -> tuple[str, str, str, str]:
    settings = dataset.get("connectionSettings") or {}
    properties = settings.get("properties") or {}
    references = dataset.get("externalReferences") or properties.get("externalReferences") or {}
    type_properties = properties.get("typeProperties") or {}
    return (
        str(settings.get("name") or "").strip(),
        str(properties.get("type") or "").strip(),
        str(references.get("connection") or "").strip(),
        str(type_properties.get("rootFolder") or "").strip(),
    )


def _pipeline_endpoint(payload: dict) -> Optional[dict[str, str]]:
    """Extract a table or file endpoint from Fabric Copy/Lookup settings."""
    dataset = payload.get("datasetSettings") or {}
    if not isinstance(dataset, dict):
        return None
    properties = dataset.get("typeProperties") or {}
    if not isinstance(properties, dict):
        properties = {}
    location = properties.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    connection_name, connection_type, connection_id, root_folder = _pipeline_connection(dataset)
    store_settings = payload.get("storeSettings") or {}
    signature = " ".join(
        str(value or "")
        for value in (
            payload.get("type"),
            store_settings.get("type") if isinstance(store_settings, dict) else "",
            dataset.get("type"),
            location.get("type"),
            connection_type,
        )
    ).casefold()
    is_lakehouse = "lakehouse" in signature
    if is_lakehouse:
        platform = LAKEHOUSE_PLATFORM.get(connection_name.casefold(), "FABRIC_LAKEHOUSE")
    elif any(token in signature for token in ("azuresql", "sqldw", "azureblob", "azureblobfs")):
        platform = "AZURE"
    else:
        platform = "FILE"

    table, table_dynamic = _pipeline_value(properties.get("table"))
    schema, schema_dynamic = _pipeline_value(properties.get("schema"))
    database, database_dynamic = _pipeline_value(properties.get("database"))
    ambiguous = table_dynamic or schema_dynamic or database_dynamic
    if table:
        schema_parts = []
        if is_lakehouse and connection_name:
            schema_parts.append(connection_name)
        if database:
            schema_parts.append(database)
        if schema:
            schema_parts.append(schema)
        endpoint_schema = ".".join(schema_parts) or connection_name or "UNKNOWN"
        endpoint_object = table
        endpoint_type = "TABLE"
    else:
        path, path_dynamic = _pipeline_location_path(location)
        ambiguous = ambiguous or path_dynamic
        if not path:
            return None
        file_system, filesystem_dynamic = _pipeline_value(location.get("fileSystem"))
        ambiguous = ambiguous or filesystem_dynamic
        if is_lakehouse:
            endpoint_schema = ".".join(
                part for part in (connection_name, root_folder or "Files") if part
            ) or "Lakehouse.Files"
        else:
            endpoint_schema = file_system or connection_name or "FILE"
        endpoint_object = path
        endpoint_type = "FILE"

    return {
        "schema": endpoint_schema,
        "object": endpoint_object,
        "type": endpoint_type,
        "platform": platform,
        "external_location": f"connection:{connection_id}" if connection_id else "NULL",
        "ambiguous": "Yes" if ambiguous else "No",
    }


def _pipeline_activity_node(
    pipeline_name: str,
    activity: dict,
    activity_path: str,
    notebook_catalog: dict[str, dict[str, str]],
) -> dict[str, str]:
    activity_type = str(activity.get("type") or "Activity").strip()
    if activity_type.casefold() == "tridentnotebook":
        notebook_id = str((activity.get("typeProperties") or {}).get("notebookId") or "").strip()
        notebook = notebook_catalog.get(notebook_id.casefold())
        return {
            "schema": "FabricNotebook",
            "object": notebook["name"] if notebook else str(activity.get("name") or activity_path),
            "type": "NOTEBOOK",
            "platform": "FABRIC_NOTEBOOK",
            "ambiguous": "No" if notebook else "Yes",
        }
    token = re.sub(r"[^A-Za-z0-9]+", "_", activity_type).strip("_").upper()
    return {
        "schema": pipeline_name,
        "object": activity_path,
        "type": f"{token or 'PIPELINE'}_ACTIVITY",
        "platform": "FABRIC_PIPELINE",
        "ambiguous": "No",
    }


def _pipeline_row(
    source: dict[str, str],
    target: dict[str, str],
    dependency_type: str,
    evidence: str,
    pipeline_name: str,
    activity_name: str,
    parameter: str = "*",
) -> dict[str, str]:
    local_platforms = {
        "FABRIC_BRONZE", "FABRIC_SILVER", "FABRIC_GOLD", "FABRIC_LAKEHOUSE",
        "FABRIC_PIPELINE", "FABRIC_NOTEBOOK",
    }
    return {
        "Source_Schema": source["schema"],
        "Source_Object": source["object"],
        "Source_Type": source["type"],
        "Target_Schema": target["schema"],
        "Target_Object": target["object"],
        "Target_Type": target["type"],
        "Dependency_Type": dependency_type,
        "Column_Or_Parameter": parameter,
        "Schema_Bound": "No",
        "External_Location": source.get("external_location", "NULL"),
        "Is_Ambiguous": "Yes" if (
            source.get("ambiguous") == "Yes" or target.get("ambiguous") == "Yes"
        ) else "No",
        "Location_Type": "LOCAL" if source["platform"] in local_platforms else "REMOTE",
        "Source_Platform": source["platform"],
        "Target_Platform": target["platform"],
        "Transform_Tool": "FABRIC_PIPELINE",
        "Transform_Name": f"{pipeline_name}.{activity_name}",
        "Transform_Path": evidence,
        "Source_Column": "NULL",
    }


def _flatten_pipeline_activities(activities: list, parent_path: str = "") -> list[tuple]:
    flattened = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        name = str(activity.get("name") or activity.get("type") or "Activity").strip()
        activity_path = f"{parent_path}/{name}" if parent_path else name
        flattened.append((activity_path, activity, parent_path))
        nested = (activity.get("typeProperties") or {}).get("activities") or []
        if isinstance(nested, list):
            flattened.extend(_flatten_pipeline_activities(nested, activity_path))
    return flattened


def parse_pipelines(root: Path, stats: Counter, artifact_root: Optional[Path] = None) -> list:
    rows = []
    notebook_catalog = _pipeline_item_catalog(artifact_root or root, "Notebook")
    for content_path in sorted(root.rglob("*.DataPipeline/pipeline-content.json")):
        if _is_ignored(content_path, root):
            continue
        stats["pipelines"] += 1
        try:
            definition = json.loads(read_text(content_path))
        except (json.JSONDecodeError, OSError):
            stats["pipelines_unreadable"] += 1
            continue
        activities = (definition.get("properties") or {}).get("activities") or []
        if not isinstance(activities, list):
            stats["pipelines_invalid_activities"] += 1
            continue

        pipeline_name = _pipeline_display_name(content_path.parent)
        evidence = rel_path(content_path)
        pipeline_node = {
            "schema": "FabricPipeline",
            "object": pipeline_name,
            "type": "PIPELINE",
            "platform": "FABRIC_PIPELINE",
            "ambiguous": "No",
        }
        flattened = _flatten_pipeline_activities(activities)
        stats["pipeline_activities"] += len(flattened)
        nodes = {
            activity_path: _pipeline_activity_node(
                pipeline_name, activity, activity_path, notebook_catalog
            )
            for activity_path, activity, _ in flattened
        }
        scoped_nodes = {
            (parent_path, str(activity.get("name") or "").strip()): nodes[activity_path]
            for activity_path, activity, parent_path in flattened
            if str(activity.get("name") or "").strip()
        }

        for activity_path, activity, parent_path in flattened:
            activity_name = str(activity.get("name") or activity_path).strip()
            activity_type = str(activity.get("type") or "").casefold()
            activity_node = nodes[activity_path]
            parent_node = nodes.get(parent_path, pipeline_node)
            rows.append(_pipeline_row(
                parent_node,
                activity_node,
                "ORCHESTRATION",
                evidence,
                pipeline_name,
                activity_name,
                "NESTED_ACTIVITY" if parent_path else "ACTIVITY",
            ))
            stats["pipeline_orchestration_rows"] += 1

            for dependency in activity.get("dependsOn") or []:
                if not isinstance(dependency, dict):
                    continue
                dependency_name = str(dependency.get("activity") or "").strip()
                dependency_node = scoped_nodes.get((parent_path, dependency_name))
                if dependency_node is None:
                    stats["pipeline_dependencies_unresolved"] += 1
                    continue
                conditions = dependency.get("dependencyConditions") or []
                rows.append(_pipeline_row(
                    dependency_node,
                    activity_node,
                    "ORCHESTRATION",
                    evidence,
                    pipeline_name,
                    activity_name,
                    ",".join(str(condition) for condition in conditions) or "DEPENDENCY",
                ))
                stats["pipeline_dependency_rows"] += 1

            type_properties = activity.get("typeProperties") or {}
            if activity_type == "copy":
                source = _pipeline_endpoint(type_properties.get("source") or {})
                target = _pipeline_endpoint(type_properties.get("sink") or {})
                if source and target:
                    rows.append(_pipeline_row(
                        source, target, "DATA_FLOW", evidence, pipeline_name, activity_name
                    ))
                    stats["pipeline_copy_rows"] += 1
                else:
                    stats["pipeline_copy_endpoints_unresolved"] += 1
            elif activity_type == "lookup":
                source_payload = dict(type_properties.get("source") or {})
                source_payload.setdefault(
                    "datasetSettings", type_properties.get("datasetSettings") or {}
                )
                source = _pipeline_endpoint(source_payload)
                if source:
                    rows.append(_pipeline_row(
                        source,
                        activity_node,
                        "DATA_FLOW",
                        evidence,
                        pipeline_name,
                        activity_name,
                    ))
                    stats["pipeline_lookup_rows"] += 1
                else:
                    stats["pipeline_lookup_endpoints_unresolved"] += 1

            if activity_type == "tridentnotebook":
                metric = (
                    "pipeline_notebooks_resolved"
                    if activity_node["ambiguous"] == "No"
                    else "pipeline_notebooks_unresolved"
                )
                stats[metric] += 1
    return rows


# ─────────────────────────── output ───────────────────────────
def disambiguate_bronze_collisions(bronze_rows: list, other_rows: list, stats: Counter) -> None:
    """A Bronze table and a Silver/Gold table can share `schema.object` but are different tables.

    Node identity is `schema.object`, so the Bronze one is qualified with its lakehouse to stop
    the two collapsing into a single node (e.g. the static xlsx feeding the Silver table).
    """
    produced_elsewhere = {(r["Target_Schema"], r["Target_Object"]) for r in other_rows}
    collisions = {(r["Target_Schema"], r["Target_Object"]) for r in bronze_rows} & produced_elsewhere
    if not collisions:
        return
    stats["bronze_name_collisions"] += len(collisions)
    for row in bronze_rows + other_rows:
        if (row["Target_Platform"] == "FABRIC_BRONZE"
                and (row["Target_Schema"], row["Target_Object"]) in collisions):
            row["Target_Schema"] = f"bronze_lh.{row['Target_Schema']}"
        if (row["Source_Platform"] == "FABRIC_BRONZE"
                and (row["Source_Schema"], row["Source_Object"]) in collisions):
            row["Source_Schema"] = f"bronze_lh.{row['Source_Schema']}"


def write_csv(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped = deduplicate_rows(rows)
    deduped.sort(key=lambda r: (r["Target_Schema"], r["Target_Object"],
                                r["Source_Schema"], r["Source_Object"]))
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(deduped)
    return len(deduped)


def deduplicate_rows(rows) -> list[dict]:
    """Return unique rows without writing output files."""
    return list({tuple(row[column] for column in CSV_HEADER): row for row in rows}.values())


def _artifact_counts(layout: RepositoryLayout) -> dict:
    return {
        "bronze_notebooks": len(notebook_files(layout.bronze_notebooks)),
        "silver_notebooks": len(notebook_files(layout.silver_notebooks)),
        "gold_notebooks": len(notebook_files(layout.gold_notebooks)),
        "pipelines": len(list(layout.pipelines_root.rglob("*.DataPipeline/pipeline-content.json"))),
        "semantic_models": len(list(layout.semantic_models_root.rglob("*.SemanticModel"))),
        "reports": len(list(layout.reports_root.rglob("*.Report/definition.pbir"))),
        "shortcuts": len(layout.shortcut_metadata),
        "config_files": sum(path.is_file() for path in (
            layout.sources_config,
            layout.static_files_config,
            layout.redshift_direct_config,
        )),
    }


def _parse_active_repository() -> tuple[dict[str, list[dict]], Counter, PlatformResolver]:
    """Parse the repository currently loaded into the module path configuration."""
    stats = Counter()
    resolver = PlatformResolver()
    seed_resolver(
        resolver,
        BRONZE_NOTEBOOK_ROOTS + SILVER_NOTEBOOK_ROOTS + GOLD_NOTEBOOK_ROOTS,
    )
    shortcuts = load_shortcuts(stats)
    for shortcut_ref, _, _ in shortcuts:
        resolver.observe(shortcut_ref)

    silver_rows = parse_notebooks(SILVER_NOTEBOOK_ROOTS, "silver", resolver, stats)
    gold_rows = (parse_notebooks(GOLD_NOTEBOOK_ROOTS, "gold", resolver, stats)
                 + shortcut_rows(shortcuts, resolver))
    semantic_rows = parse_semantic_models(SEMANTIC_MODELS, resolver, stats)
    known_models = {
        row["Target_Object"]
        for row in semantic_rows
        if row["Target_Type"] == "SEMANTIC_MODEL"
    }
    report_rows = parse_reports(REPORTS_ROOT, known_models, resolver, stats)
    pipeline_rows = parse_pipelines(PIPELINES_ROOT, stats, FABRIC_ROOT)
    bronze_rows = (parse_notebooks(BRONZE_NOTEBOOK_ROOTS, "bronze", resolver, stats)
                   + parse_bronze(SOURCES_CONFIG, resolver, stats)
                   + parse_bronze_enrichment_lookups(SOURCES_CONFIG, resolver, stats)
                   + parse_bronze_derived_queries(BRONZE_NOTEBOOK_ROOTS, resolver, stats)
                   + parse_static_files(STATIC_FILES_CONFIG, resolver, stats)
                   + parse_redshift_direct(REDSHIFT_DIRECT_CONFIG, resolver, stats))
    disambiguate_bronze_collisions(
        bronze_rows,
        silver_rows + gold_rows + semantic_rows,
        stats,
    )
    return {
        "bronze": bronze_rows,
        "silver": silver_rows,
        "gold": gold_rows,
        "pipelines": pipeline_rows,
        "semantic_models": semantic_rows,
        "reports": report_rows,
    }, stats, resolver


def parse_repository(repo_root: Path,
                     config_path: Optional[Path] = None) -> tuple[
                         RepositoryLayout,
                         dict,
                         dict[str, list[dict]],
                         Counter,
                         PlatformResolver,
                     ]:
    """Discover and parse a local Fabric repository without writing CSV files."""
    layout = discover_repository(repo_root, config_path)
    activate_repository(layout)
    discovered = _artifact_counts(layout)
    if not any(discovered.values()):
        raise ValueError(
            "No supported Fabric artifacts were discovered. "
            "Check the repository path or add lineage.config.json."
        )
    rows_by_layer, stats, resolver = _parse_active_repository()
    return layout, discovered, rows_by_layer, stats, resolver


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo_root", nargs="?", type=Path,
        help="Local cloned repository to scan. Existing no-argument behaviour is retained.",
    )
    parser.add_argument(
        "--config", type=Path,
        help="Optional lineage.config.json path; relative paths are resolved from repo_root.",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)

    layout = None
    discovered_counts = None
    rows_by_layer = None
    stats = None
    resolver = None
    if args.repo_root is not None or args.config is not None:
        repo_root = args.repo_root or WORKSPACE
        try:
            layout, discovered_counts, rows_by_layer, stats, resolver = parse_repository(
                repo_root,
                args.config,
            )
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            parser.error(str(exc))

        print(f"Repository root: {layout.repo_root}")
        print(f"Artifact root:   {layout.artifact_root}")
        print(f"Configuration:   {layout.profile_path or 'automatic discovery'}")
        print("Discovered inputs:")
        for name, count in discovered_counts.items():
            print(f"  {name:20} {count:>6}")
        for warning in layout.warnings:
            print(f"Warning: {warning}", file=sys.stderr)

    if args.output_dir is not None and layout is not None and not args.output_dir.is_absolute():
        out = layout.repo_root / args.output_dir
    else:
        out = args.output_dir or (layout.output_dir if layout is not None else OUTPUT_DIR)
    if rows_by_layer is None or stats is None or resolver is None:
        rows_by_layer, stats, resolver = _parse_active_repository()

    counts = {
        "bronze": write_csv(out / BRONZE_CSV.name, rows_by_layer["bronze"]),
        "silver": write_csv(out / SILVER_CSV.name, rows_by_layer["silver"]),
        "gold": write_csv(out / GOLD_CSV.name, rows_by_layer["gold"]),
        "pipelines": write_csv(out / PIPELINES_CSV.name, rows_by_layer["pipelines"]),
        "semantic_models": write_csv(
            out / SEMANTIC_CSV.name,
            rows_by_layer["semantic_models"],
        ),
        "reports": write_csv(out / REPORTS_CSV.name, rows_by_layer["reports"]),
    }
    counts["consolidated"] = write_csv(
        out / CONSOLIDATED_CSV.name,
        [row for rows in rows_by_layer.values() for row in rows],
    )

    print(f"Output directory: {out}")
    for name, count in counts.items():
        print(f"  {name:16} {count:>6} rows")
    print("Scan stats:")
    for key in sorted(stats):
        print(f"  {key:34} {stats[key]}")
    print(f"  silver_schemas_detected            {len(resolver.silver_schemas)}")
    print(f"  bronze_schemas_detected            {len(resolver.bronze_schemas)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
