#!/usr/bin/env python3
import csv
import collections
from pathlib import Path


WORKSPACE = Path(r"c:\Repos\WK LR US Artifacts")
INVENTORY_CSV = WORKSPACE / "csv" / "talend_pipelines_inventory.csv"
LINEAGE_CSV = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "lineage_metadata_talend_inventory.csv"
OUT_CSV = WORKSPACE / "silver_agent" / "build" / "lineage-viewer" / "lineage-output" / "talend_subfolder_coverage_summary.csv"


PLATFORM_MAP = {
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


def folder_of(path: str) -> str:
    p = path.replace("\\", "/")
    parts = p.split("/")
    try:
        i = parts.index("process")
        if i + 1 < len(parts) - 1:
            return parts[i + 1]
    except ValueError:
        pass
    return "(root)"


def split_components(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(";") if c.strip()]


def main() -> None:
    inventory = list(csv.DictReader(open(INVENTORY_CSV, encoding="utf-8-sig")))
    lineage = list(csv.DictReader(open(LINEAGE_CSV, encoding="utf-8")))

    lineage_by_path = collections.defaultdict(list)
    for row in lineage:
        lineage_by_path[row["Transform_Path"]].append(row)

    stats = collections.defaultdict(
        lambda: {
            "total": 0,
            "with_rows": 0,
            "without_rows": 0,
            "rows": 0,
            "src_plat": set(),
            "tgt_plat": set(),
            "expected_src_plat": set(),
            "orchestration_like": 0,
        }
    )

    for rec in inventory:
        rel_path = rec["relative_item_path"].replace("\\", "/")
        subfolder = folder_of(rel_path)
        st = stats[subfolder]
        st["total"] += 1

        rows = lineage_by_path.get(rel_path, [])
        if rows:
            st["with_rows"] += 1
            st["rows"] += len(rows)
            for lr in rows:
                st["src_plat"].add(lr["Source_Platform"])
                st["tgt_plat"].add(lr["Target_Platform"])
        else:
            st["without_rows"] += 1

        src_comps = split_components(rec.get("source_components", ""))
        tgt_comps = split_components(rec.get("target_components", ""))
        all_comps = split_components(rec.get("components", ""))

        if not src_comps:
            src_comps = all_comps

        if tgt_comps:
            for comp in src_comps:
                plat = PLATFORM_MAP.get(comp.lower())
                if not plat:
                    continue
                if plat == "API" and "/eloqua/" in rel_path.lower():
                    plat = "ELOQUA"
                st["expected_src_plat"].add(plat)
        else:
            low = ";".join(all_comps).lower()
            if any(x in low for x in ["trunjob", "tprejob", "tpostjob"]) and not split_components(rec.get("source_components", "")):
                st["orchestration_like"] += 1

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "subfolder",
                "pipelines_total",
                "pipelines_with_lineage",
                "pipelines_without_lineage",
                "coverage_pct",
                "lineage_rows",
                "source_platforms_in_lineage",
                "target_platforms_in_lineage",
                "expected_source_platforms_from_inventory",
                "missing_expected_source_platforms",
                "orchestration_like_without_targets",
            ]
        )
        for subfolder in sorted(stats):
            st = stats[subfolder]
            missing = sorted(st["expected_src_plat"] - st["src_plat"])
            cov = (100.0 * st["with_rows"] / st["total"]) if st["total"] else 0.0
            writer.writerow(
                [
                    subfolder,
                    st["total"],
                    st["with_rows"],
                    st["without_rows"],
                    f"{cov:.1f}",
                    st["rows"],
                    ";".join(sorted(st["src_plat"])),
                    ";".join(sorted(st["tgt_plat"])),
                    ";".join(sorted(st["expected_src_plat"])),
                    ";".join(missing),
                    st["orchestration_like"],
                ]
            )

    total = sum(v["total"] for v in stats.values())
    covered = sum(v["with_rows"] for v in stats.values())
    print(f"Wrote {OUT_CSV}")
    print(f"Subfolders: {len(stats)}")
    print(f"Pipelines: {total}, with lineage: {covered}, without lineage: {total-covered}, coverage: {100*covered/total:.1f}%")
    print("Per-subfolder quick summary:")
    for subfolder in sorted(stats):
        st = stats[subfolder]
        cov = (100.0 * st["with_rows"] / st["total"]) if st["total"] else 0.0
        plats = ",".join(sorted(st["src_plat"])) if st["src_plat"] else "-"
        print(f"  {subfolder:28} total={st['total']:3d} with={st['with_rows']:3d} without={st['without_rows']:3d} cov={cov:5.1f}% src={plats}")


if __name__ == "__main__":
    main()
