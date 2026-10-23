#!/usr/bin/env python3
"""
Build Talend lineage rows from csv/talend_pipelines_inventory.csv.

This parser is inventory-driven (does not parse .item XML) and emits rows
in the standard lineage schema used by the Lineage Viewer outputs.
"""

import csv
import re
from pathlib import Path


WORKSPACE = Path(r"c:\Repos\Scheduler")
INVENTORY_CSV = WORKSPACE / "csv" / "talend_pipelines_inventory.csv"
CALL_ANALYSIS_CSV = WORKSPACE / "analysis" / "consolidated_api_db_calls.csv"
OUTPUT_DIR = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output"
OUTPUT_FILE = OUTPUT_DIR / "lineage_metadata_talend_inventory.csv"
LEGACY_TALEND_FILE = OUTPUT_DIR / "lineage_metadata_talend_csv.csv"

CSV_HEADER = [
    "Source_Schema", "Source_Object", "Source_Type",
    "Target_Schema", "Target_Object", "Target_Type",
    "Dependency_Type", "Column_Or_Parameter", "Schema_Bound",
    "External_Location", "Is_Ambiguous", "Location_Type",
    "Source_Platform", "Target_Platform",
    "Transform_Tool", "Transform_Name", "Transform_Path",
    "Source_Column",
]


SOURCE_COMPONENT_PLATFORM = {
    "tgoogleanalyticsinput": "GOOGLE_ANALYTICS",
    "tgoogleanalytics4input": "GOOGLE_ANALYTICS",
    "tsalesforceinput": "SALESFORCE",
    "tsalesforcerecurrenceinput": "SALESFORCE",
    "tgainsightinput": "GAINSIGHT",
    "tredshiftinput": "REDSHIFT",
    "tdbinput": "REDSHIFT",
    "tjdbcinput": "REDSHIFT",
    "toracleinput": "ORACLE",
    "tmysqlinput": "MYSQL",
    "tmssqlinput": "MSSQL",
    "tfileinputdelimited": "FILE",
    "tfileinputjson": "FILE",
    "tfileinputexcel": "FILE",
    "tfileinputraw": "FILE",
    "tftpget": "FILE",
    "ts3get": "S3",
    "thttpinput": "API",
    "trestclient": "API",
    "thttprequest": "API",
    "trest": "API",
}

TARGET_COMPONENT_PLATFORM = {
    "tredshiftoutput": "REDSHIFT",
    "tredshiftbulkexec": "REDSHIFT",
    "tdboutput": "REDSHIFT",
    "tjdbcoutput": "REDSHIFT",
    "toracleoutput": "ORACLE",
    "tmysqloutput": "MYSQL",
    "tmssqloutput": "MSSQL",
    "tfileoutputdelimited": "FILE",
    "tfileoutputjson": "FILE",
    "tfileoutputxml": "FILE",
    "tfileoutputraw": "FILE",
    "ts3put": "S3",
    "ts3delete": "S3",
    "tazurebloboutput": "AZURE",
    "tsalesforceoutput": "SALESFORCE",
    "tgainsightoutput": "GAINSIGHT",
    "tftpput": "FILE",
}

EXTERNAL_PLATFORMS = {
    "GOOGLE_ANALYTICS", "SALESFORCE", "GAINSIGHT", "S3", "AZURE",
    "API", "ORACLE", "MYSQL", "MSSQL", "ELOQUA",
}


def normalize_component_name(name: str) -> str:
    return (name or "").strip().lower()


def normalize_rel_path(path: str) -> str:
    p = (path or "").replace("\\", "/").strip().lstrip("/")
    return p


def with_lrus_prefix(path: str) -> str:
    p = normalize_rel_path(path)
    if not p:
        return p
    if p.startswith("LRUS_BI/"):
        return p
    return f"LRUS_BI/{p}"


def parse_connection_field(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in (raw or "").split(";"):
        token = part.strip()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip().upper()] = value.strip()
    return values


def looks_like_data_movement_call(rec: dict) -> bool:
    category = (rec.get("Category") or "").strip().upper()
    subtype = normalize_component_name(rec.get("Subtype", ""))
    if category == "API_ENDPOINT":
        return subtype in SOURCE_COMPONENT_PLATFORM or subtype in TARGET_COMPONENT_PLATFORM
    if category == "DB_CALL":
        return subtype.endswith("input") or subtype.endswith("output") or "bulkexec" in subtype
    return False


def parse_db_object_name(database_object: str, endpoint_or_connection: str, fallback: str) -> tuple[str, str]:
    raw = (database_object or "").strip().strip('"')
    conn_parts = parse_connection_field(endpoint_or_connection)
    schema_hint = (conn_parts.get("SCHEMA_DB") or "NULL").strip().strip('"') or "NULL"

    if raw:
        clean = raw.replace('"', '').strip()
        if "." in clean:
            schema_name, object_name = clean.split(".", 1)
            return schema_name.lower() or "NULL", object_name.lower() or fallback.lower()
        return schema_hint.lower(), clean.lower()

    return schema_hint.lower(), fallback.lower()


def build_detail_spec(rec: dict, platform: str, role: str, fallback_entity: str) -> dict | None:
    endpoint = (rec.get("Endpoint_Or_Connection") or "").strip()
    db_object = (rec.get("Database_Object") or "").strip()
    object_name = (rec.get("Object_Name") or "").strip()
    fallback_name = object_name or fallback_entity

    if platform in {"ELOQUA", "API", "GOOGLE_ANALYTICS", "GAINSIGHT", "CONDUCTOR", "GONG", "LINKEDIN", "CG", "SALESFORCE"}:
        source_object = endpoint or object_name or fallback_entity
        if not source_object:
            return None
        schema_name = platform if platform != "API" else "API"
        return {
            "schema": schema_name,
            "object": source_object,
            "type": "TABLE",
            "platform": platform,
            "external_location": endpoint or "NULL",
            "ambiguous": "context." in source_object.lower() or source_object.lower() in {"unknown", ""},
            "_detail_locked": True,
        }

    if platform in {"REDSHIFT", "ORACLE", "MYSQL", "MSSQL", "AZURE"}:
        schema_name, object_token = parse_db_object_name(db_object, endpoint, fallback_name)
        return {
            "schema": schema_name.upper() if schema_name in {"S3", "FILE"} else schema_name,
            "object": object_token,
            "type": "TABLE",
            "platform": platform,
            "external_location": "NULL",
            "ambiguous": not bool(db_object),
            "_detail_locked": True,
        }

    if platform == "FILE":
        file_name = db_object or object_name or fallback_entity
        return {
            "schema": "FILE",
            "object": file_name,
            "type": "FILE",
            "platform": "FILE",
            "external_location": "NULL",
            "ambiguous": False,
            "_detail_locked": True,
        }

    if platform == "S3":
        s3_key = endpoint or db_object or object_name or fallback_entity
        return {
            "schema": "S3",
            "object": s3_key,
            "type": "S3_FILE",
            "platform": "S3",
            "external_location": endpoint or (f"s3://{s3_key}" if s3_key else "NULL"),
            "ambiguous": False,
            "_detail_locked": True,
        }

    return None


def load_call_analysis(call_analysis_csv: Path = CALL_ANALYSIS_CSV) -> dict[str, list[dict]]:
    if not call_analysis_csv.exists():
        return {}

    keyed: dict[str, list[dict]] = {}
    with open(call_analysis_csv, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            if not looks_like_data_movement_call(rec):
                continue
            rel_path = normalize_rel_path(rec.get("Relative_Path") or "")
            if not rel_path:
                continue
            keyed.setdefault(rel_path, []).append(rec)
            keyed.setdefault(with_lrus_prefix(rel_path), []).append(rec)
    return keyed


def parse_component_list(raw: str) -> list[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(";") if c.strip()]


def infer_entity_name(pipeline_name: str, display_name: str) -> str:
    original = (display_name or pipeline_name or "").strip().lower()
    name = original
    if not name:
        return "unknown_object"

    # Keep the semantic tail after LRUSLake_ when available.
    if "lruslake_" in name:
        name = name.split("lruslake_", 1)[1]

    suffixes = [
        "_delta_stage", "_stage_copy", "_stage", "_base_stage", "_base_load",
        "_load", "_base", "_etl", "_workflow", "_copy",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break

    name = re.sub(r"_+$", "", name)
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    if original.startswith("fl_ga_lruslake_") and not name.startswith("ga_"):
        name = f"ga_{name}"
    if original.startswith("fl_sf_lruslake_") and not name.startswith("sf_"):
        name = f"sf_{name}"
    return name or "unknown_object"


def infer_platforms(components: list[str], mapping: dict[str, str], folder: str) -> list[str]:
    platforms: list[str] = []
    folder_l = (folder or "").lower()
    for comp in components:
        key = comp.strip().lower()
        platform = mapping.get(key)
        if not platform:
            continue
        if platform == "API" and "/eloqua" in folder_l:
            platform = "ELOQUA"
        if platform not in platforms:
            platforms.append(platform)
    return platforms


def infer_object(platform: str, entity: str) -> tuple[str, str, str, str]:
    """Return schema, object, type, external_location defaults."""
    if platform == "REDSHIFT":
        return ("lake", entity, "TABLE", "NULL")
    if platform == "GOOGLE_ANALYTICS":
        return ("GOOGLE_ANALYTICS", entity, "TABLE", "https://analytics.google.com")
    if platform == "SALESFORCE":
        return ("SALESFORCE", entity, "TABLE", "https://login.salesforce.com")
    if platform == "GAINSIGHT":
        return ("GAINSIGHT", entity, "TABLE", "https://app.gainsight.com")
    if platform == "S3":
        s3_key = f"wklrusbi/Input/{entity}.dat"
        return ("S3", s3_key, "S3_FILE", f"s3://{s3_key}")
    if platform == "FILE":
        return ("FILE", f"{entity}.dat", "FILE", "NULL")
    if platform == "ELOQUA":
        return ("ELOQUA", entity, "TABLE", "https://secure.eloqua.com")
    if platform == "API":
        return ("API", entity, "TABLE", "NULL")
    if platform in ("ORACLE", "MYSQL", "MSSQL", "AZURE"):
        return (platform, entity, "TABLE", "NULL")
    return ("NULL", entity, "TABLE", "NULL")


def make_generic_spec(platform: str, entity: str) -> dict:
    schema, obj, obj_type, ext_loc = infer_object(platform, entity)
    return {
        "schema": schema,
        "object": obj,
        "type": obj_type,
        "platform": platform,
        "external_location": ext_loc,
        "ambiguous": True,
    }


def make_row_from_specs(src: dict, tgt: dict, transform_name: str, transform_path: str) -> dict:
    src_platform = src["platform"]
    tgt_platform = tgt["platform"]
    location_type = "EXTERNAL" if (
        src_platform in EXTERNAL_PLATFORMS or tgt_platform in EXTERNAL_PLATFORMS
    ) else "LOCAL"

    return {
        "Source_Schema": src["schema"],
        "Source_Object": src["object"],
        "Source_Type": src["type"],
        "Target_Schema": tgt["schema"],
        "Target_Object": tgt["object"],
        "Target_Type": tgt["type"],
        "Dependency_Type": "DATA_FLOW",
        "Column_Or_Parameter": "*",
        "Schema_Bound": "No",
        "External_Location": src.get("external_location", "NULL"),
        "Is_Ambiguous": "Yes" if (src.get("ambiguous") or tgt.get("ambiguous")) else "No",
        "Location_Type": location_type,
        "Source_Platform": src_platform,
        "Target_Platform": tgt_platform,
        "Transform_Tool": "TALEND",
        "Transform_Name": transform_name,
        "Transform_Path": transform_path,
        "Source_Column": "NULL",
        "_detail_locked": src.get("_detail_locked") or tgt.get("_detail_locked"),
    }


def make_row(src_platform: str, tgt_platform: str, entity: str,
             transform_name: str, transform_path: str) -> dict:
    return make_row_from_specs(
        make_generic_spec(src_platform, entity),
        make_generic_spec(tgt_platform, entity),
        transform_name,
        transform_path,
    )


def row_key(row: dict) -> tuple:
    return (
        row["Source_Schema"], row["Source_Object"], row["Source_Type"],
        row["Target_Schema"], row["Target_Object"], row["Target_Type"],
        row["Column_Or_Parameter"], row["Source_Platform"], row["Target_Platform"],
        row["Transform_Name"], row["Transform_Path"], row["Source_Column"],
    )


def canonical_key(row: dict) -> tuple:
    """
    Coarse key used to align inventory rows with canonical Talend rows from
    the legacy parser when both describe the same flow edge.
    """
    return (
        row.get("Transform_Path", ""),
        row.get("Transform_Name", ""),
        row.get("Source_Platform", ""),
        row.get("Target_Platform", ""),
    )


def load_legacy_canonical_rows() -> dict[tuple, list[dict]]:
    """Load legacy Talend rows keyed by transform/path/platform pair."""
    if not LEGACY_TALEND_FILE.exists():
        return {}

    keyed: dict[tuple, list[dict]] = {}
    with open(LEGACY_TALEND_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = canonical_key(row)
            keyed.setdefault(key, []).append(row)
    return keyed


def apply_legacy_name_overrides(rows: list[dict]) -> list[dict]:
    """
    Keep inventory coverage, but when the legacy Talend parser has an exact
    row for the same transform/path/platform edge, copy canonical names so
    downstream attached files use expected object naming.
    """
    legacy = load_legacy_canonical_rows()
    if not legacy:
        return rows

    patched: list[dict] = []
    for row in rows:
        if row.get("_detail_locked"):
            patched.append(row)
            continue
        key = canonical_key(row)
        matches = legacy.get(key, [])
        if not matches:
            patched.append(row)
            continue

        # Prefer exact source/target object match if present; otherwise take first.
        selected = matches[0]
        for cand in matches:
            if (
                cand.get("Source_Object", "").lower() == row.get("Source_Object", "").lower()
                or cand.get("Target_Object", "").lower() == row.get("Target_Object", "").lower()
            ):
                selected = cand
                break

        merged = dict(row)
        merged["Source_Schema"] = selected.get("Source_Schema", merged["Source_Schema"])
        merged["Source_Object"] = selected.get("Source_Object", merged["Source_Object"])
        merged["Source_Type"] = selected.get("Source_Type", merged["Source_Type"])
        merged["Target_Schema"] = selected.get("Target_Schema", merged["Target_Schema"])
        merged["Target_Object"] = selected.get("Target_Object", merged["Target_Object"])
        merged["Target_Type"] = selected.get("Target_Type", merged["Target_Type"])
        if selected.get("External_Location"):
            merged["External_Location"] = selected["External_Location"]
        patched.append(merged)

    return patched


def build_rows(inventory_csv: Path = INVENTORY_CSV,
               call_analysis_csv: Path = CALL_ANALYSIS_CSV) -> list[dict]:
    rows: list[dict] = []
    call_analysis = load_call_analysis(call_analysis_csv)
    with open(inventory_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for rec in reader:
            pipeline_name = (rec.get("pipeline_name") or "").strip()
            display_name = (rec.get("display_name") or pipeline_name).strip()
            rel_path = normalize_rel_path(rec.get("relative_item_path") or "NULL")
            folder = (rec.get("folder") or "").strip()

            src_components = parse_component_list(rec.get("source_components", ""))
            tgt_components = parse_component_list(rec.get("target_components", ""))
            all_components = parse_component_list(rec.get("components", ""))

            src_platforms = infer_platforms(src_components, SOURCE_COMPONENT_PLATFORM, folder)
            tgt_platforms = infer_platforms(tgt_components, TARGET_COMPONENT_PLATFORM, folder)

            # If source/target columns are empty in inventory, infer from complete component list.
            if not src_platforms:
                src_platforms = infer_platforms(all_components, SOURCE_COMPONENT_PLATFORM, folder)
            if not tgt_platforms:
                tgt_platforms = infer_platforms(all_components, TARGET_COMPONENT_PLATFORM, folder)

            if not src_platforms and not tgt_platforms:
                continue

            entity = infer_entity_name(pipeline_name, display_name)
            call_rows = call_analysis.get(rel_path, [])

            detailed_source_specs: list[dict] = []
            detailed_target_specs: list[dict] = []
            for call_row in call_rows:
                subtype = normalize_component_name(call_row.get("Subtype", ""))
                src_platform = infer_platforms([subtype], SOURCE_COMPONENT_PLATFORM, folder)
                tgt_platform = infer_platforms([subtype], TARGET_COMPONENT_PLATFORM, folder)
                for platform in src_platform:
                    spec = build_detail_spec(call_row, platform, "source", entity)
                    if spec:
                        detailed_source_specs.append(spec)
                for platform in tgt_platform:
                    spec = build_detail_spec(call_row, platform, "target", entity)
                    if spec:
                        detailed_target_specs.append(spec)

            # Standard direct links from known source->target component sets.
            for src_plat in src_platforms:
                src_specs = [s for s in detailed_source_specs if s["platform"] == src_plat] or [make_generic_spec(src_plat, entity)]
                for tgt_plat in tgt_platforms:
                    if src_plat == tgt_plat:
                        continue
                    tgt_specs = [t for t in detailed_target_specs if t["platform"] == tgt_plat] or [make_generic_spec(tgt_plat, entity)]
                    for src_spec in src_specs:
                        for tgt_spec in tgt_specs:
                            rows.append(make_row_from_specs(src_spec, tgt_spec, display_name, rel_path))

            # Synthesize common load pattern when inventory only has targets.
            if not src_platforms and "REDSHIFT" in tgt_platforms and "S3" in tgt_platforms:
                rows.append(make_row("FILE", "S3", entity, display_name, rel_path))
                rows.append(make_row("S3", "REDSHIFT", entity, display_name, rel_path))

    # De-duplicate
    seen: set[tuple] = set()
    unique_rows: list[dict] = []
    for row in rows:
        k = row_key(row)
        if k in seen:
            continue
        seen.add(k)
        row.pop("_detail_locked", None)
        unique_rows.append(row)
    return unique_rows


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    rows = apply_legacy_name_overrides(rows)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Inventory rows parsed: {sum(1 for _ in open(INVENTORY_CSV, encoding='utf-8-sig')) - 1}")
    print(f"Lineage rows written:  {len(rows)}")
    print(f"Output:                {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
