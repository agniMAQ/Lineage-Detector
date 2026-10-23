"""Static parser registry and most-specific ownership resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from inventory import Inventory, InventoryCandidate
from parser_adapters import AdapterResult, ParserContext
from parser_adapters import parse_fabric, parse_powerbi, parse_python, parse_sql, parse_tableau, parse_talend


AdapterCallable = Callable[
    [ParserContext, tuple[InventoryCandidate, ...]],
    AdapterResult,
]


@dataclass(frozen=True)
class ParserSpec:
    parser_id: str
    output_filename: str
    priority: int
    signatures: frozenset[str]
    adapter: AdapterCallable

    def matches(self, candidate: InventoryCandidate) -> bool:
        return candidate.signature in self.signatures


@dataclass(frozen=True)
class SelectionResult:
    assignments: dict[str, tuple[InventoryCandidate, ...]]
    unsupported: tuple[InventoryCandidate, ...]


PARSER_REGISTRY = (
    ParserSpec(
        "fabric",
        "lineage_metadata_fabric.csv",
        100,
        frozenset({"fabric-artifact"}),
        parse_fabric,
    ),
    ParserSpec(
        "talend",
        "lineage_metadata_talend.csv",
        90,
        frozenset({"talend-item", "talend-inventory", "talend-call-analysis"}),
        parse_talend,
    ),
    ParserSpec(
        "tableau",
        "lineage_metadata_tableau.csv",
        80,
        frozenset({"tableau-metadata", "tableau-report-inventory"}),
        parse_tableau,
    ),
    ParserSpec(
        "powerbi",
        "lineage_metadata_powerbi.csv",
        80,
        frozenset({"powerbi-lineage"}),
        parse_powerbi,
    ),
    ParserSpec(
        "sql",
        "lineage_metadata_sql.csv",
        60,
        frozenset({"standalone-sql", "redshift-view-inventory"}),
        parse_sql,
    ),
    ParserSpec(
        "python",
        "lineage_metadata_python.csv",
        50,
        frozenset({"python-source", "python-notebook"}),
        parse_python,
    ),
)


def registry_by_id() -> dict[str, ParserSpec]:
    return {spec.parser_id: spec for spec in PARSER_REGISTRY}


def select_parsers(
    inventory: Inventory,
    registry: tuple[ParserSpec, ...] = PARSER_REGISTRY,
) -> SelectionResult:
    mutable_assignments: dict[str, list[InventoryCandidate]] = {}
    unsupported: list[InventoryCandidate] = []

    for candidate in inventory.candidates:
        matches = [spec for spec in registry if spec.matches(candidate)]
        if not matches:
            if candidate.likely_lineage:
                unsupported.append(candidate)
            continue
        winner = sorted(matches, key=lambda spec: (-spec.priority, spec.parser_id))[0]
        mutable_assignments.setdefault(winner.parser_id, []).append(candidate)

    assignments = {
        parser_id: tuple(candidates)
        for parser_id, candidates in sorted(mutable_assignments.items())
    }
    return SelectionResult(assignments, tuple(unsupported))
