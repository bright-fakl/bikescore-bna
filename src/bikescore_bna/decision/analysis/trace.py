"""Per-feature decision trace (design-review A.7).

Evaluate a decision for one road/node in *explain* mode: every clause records its
✓/✗ outcome, every rule whether it matched, and which output column(s) it won;
each pass reports the field values it produced. Supersedes the retired
``diagnostics.py`` trace (which targeted the old results-dict model).

Derived fields are materialized per pass (matching ``run_pass``), so a clause on a
provider-supplied field (e.g. ``has_high_cross_ft``) traces against its real value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from bikescore_bna.decision.catalog import FieldCatalog, materialize_fields
from bikescore_bna.decision.model import Decision


@dataclass
class ClauseTrace:
    field: str
    op: str
    value: Any
    actual: Any
    ok: bool


@dataclass
class RuleTrace:
    id: str
    matched: bool
    clauses: list[ClauseTrace]
    won: list[str]  # output columns this rule won (first match for them)
    source: Mapping[str, Any] | None = None


@dataclass
class PassTrace:
    name: str
    rules: list[RuleTrace]
    default_won: list[str]  # columns filled by the table default
    produced: dict[str, Any]  # field -> value after this pass


@dataclass
class FeatureTrace:
    index: Any
    passes: list[PassTrace] = field(default_factory=list)

    def winning_rule(self, output: str) -> str | None:
        """The id of the last rule/pass to set ``output`` (the effective winner)."""
        winner: str | None = None
        for p in self.passes:
            for r in p.rules:
                if output in r.won:
                    winner = r.id
            if output in p.default_won:
                winner = f"{p.name}:default"
        return winner


def trace(
    decision: Decision,
    frame: pd.DataFrame,
    catalog: FieldCatalog | None = None,
    *,
    index: Any = None,
    extras: Mapping[str, Any] | None = None,
) -> FeatureTrace:
    """Trace ``decision`` for one row of ``frame`` (default: the first row)."""
    if index is None:
        index = frame.index[0]
    work = frame.copy()
    result = FeatureTrace(index=index)

    for p in decision.passes:
        if catalog is not None:
            try:
                work = materialize_fields(work, p.table.referenced_fields(), catalog, extras)
            except Exception:
                pass  # provider unavailable (e.g. no node_attrs) — trace what we have
        before = work.loc[index].to_dict()

        won_cols: dict[str, str] = {}
        rule_traces: list[RuleTrace] = []
        for rule in p.table.rules:
            cts = [
                ClauseTrace(c.field, c.op, c.value, before.get(c.field), c.matches(before))
                for c in rule.when
            ]
            matched = all(ct.ok for ct in cts)
            won: list[str] = []
            if matched:
                for col in rule.set:
                    if col not in won_cols:
                        won_cols[col] = rule.id
                        won.append(col)
            rule_traces.append(
                RuleTrace(id=rule.id, matched=matched, clauses=cts, won=won, source=rule.source)
            )

        work = p.table.apply(work)
        after = work.loc[index].to_dict()
        default_won = [
            c for c in (p.table.default or {}) if c not in won_cols
        ]
        produced = {c: after.get(c) for c in p.table.output_fields()}
        result.passes.append(
            PassTrace(name=p.name, rules=rule_traces, default_won=default_won, produced=produced)
        )
    return result
