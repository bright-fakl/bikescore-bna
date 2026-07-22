"""Coverage / never-fired analysis (design-review A.7).

Tally the winning rule for each output column over a dataset, aggregating expanded
sweep rules back to their **authored** id via ``Rule.source``. Reports per-rule
win counts, default usage, fall-through (no rule, no default), and the set of
authored rules that never fired. *Default-analysis* (e.g. speed: % maxspeed vs city
vs state vs FC-default) is just coverage over the compiled resolution-chain rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from bikescore_bna.decision.catalog import FieldCatalog, materialize_fields
from bikescore_bna.decision.model import Decision, DecisionTable, Rule


def authored_id(rule: Rule) -> str:
    """The authored rule id (strips sweep expansion), via ``source`` provenance."""
    if rule.source and "id" in rule.source:
        return str(rule.source["id"])
    return rule.id


@dataclass
class ColumnCoverage:
    column: str
    counts: dict[str, int]  # authored rule id -> rows won
    default_count: int
    fallthrough_count: int
    total: int


@dataclass
class PassCoverage:
    name: str
    columns: list[ColumnCoverage]


@dataclass
class CoverageReport:
    passes: list[PassCoverage] = field(default_factory=list)
    never_fired: list[str] = field(default_factory=list)  # authored ids, declared but unused
    total_rows: int = 0

    def counts(self) -> dict[str, int]:
        """Flat authored-rule → total wins across all passes/columns."""
        out: dict[str, int] = {}
        for p in self.passes:
            for col in p.columns:
                for rid, n in col.counts.items():
                    out[rid] = out.get(rid, 0) + n
        return out


def _column_coverage(table: DecisionTable, col: str, df: pd.DataFrame) -> ColumnCoverage:
    rules = [r for r in table.rules if col in r.set]
    counts: dict[str, int] = {}
    assigned = np.zeros(len(df), dtype=bool)
    for rule in rules:
        mask = np.asarray(rule.row_mask(df)) & ~assigned
        n = int(mask.sum())
        if n:
            aid = authored_id(rule)
            counts[aid] = counts.get(aid, 0) + n
        assigned |= mask
    has_default = bool(table.default and col in table.default)
    default_count = int((~assigned).sum()) if has_default else 0
    fallthrough = int((~assigned).sum()) if not has_default else 0
    return ColumnCoverage(
        column=col, counts=counts, default_count=default_count,
        fallthrough_count=fallthrough, total=len(df),
    )


def coverage(
    decision: Decision,
    frame: pd.DataFrame,
    catalog: FieldCatalog | None = None,
    *,
    extras: Mapping[str, Any] | None = None,
) -> CoverageReport:
    """Coverage of ``decision`` over ``frame`` (every output column, every pass)."""
    work = frame.copy()
    report = CoverageReport(total_rows=len(frame))
    declared: set[str] = set()
    for p in decision.passes:
        if catalog is not None:
            try:
                work = materialize_fields(work, p.table.referenced_fields(), catalog, extras)
            except Exception:
                pass
        cols = [_column_coverage(p.table, c, work) for c in p.table.output_fields()]
        report.passes.append(PassCoverage(name=p.name, columns=cols))
        for rule in p.table.rules:
            declared.add(authored_id(rule))
        work = p.table.apply(work)

    fired = set(report.counts())
    report.never_fired = sorted(declared - fired)
    return report
