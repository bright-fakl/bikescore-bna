"""Unique decision contexts + simulation + cross-engine diff (design-review A.7).

The rules of a :class:`~bikescore.decision.model.DecisionTable` *partition* the
domain of every field they reference into the finite set of cells they can
distinguish. Two field values fall in the **same cell** iff they produce the same
truth vector across *every* clause in the table that references the field — the
exact equivalence relation the table can observe (a continuous field is therefore
split only at the thresholds rules actually use). Enumerating the cross-product of
cells and deciding each once gives an **exact, finite** characterization of the
table's behaviour even over continuous inputs.

This is the validation substrate: enumerate the cells, decide each in bna-core
*and* in the reference engine, diff the small table — the cross-engine equivalence
check that re-establishes the DC guarantee far more cheaply than per-road diffing.
``simulate`` reuses the same partition to diff two rule versions.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from bikescore.decision.model import Clause, Decision, DecisionTable
from bikescore.decision.ops import NUMERIC_OPS, SET_OPS, match_scalar

# A string guaranteed not to equal any authored literal — represents "any other
# value" for a string/enum field (the cell where no eq/in clause matches).
_OTHER = "\x00__other__"


# ── Cells & partition ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Cell:
    """One distinguishable value-class for a field: a representative ``value`` and
    the truth ``signature`` it produces across the field's clauses."""

    field: str
    label: str
    value: Any
    signature: tuple[bool, ...]


def _table_of(obj: Decision | DecisionTable, pass_name: str | None) -> DecisionTable:
    if isinstance(obj, DecisionTable):
        return obj
    if not obj.passes:
        return DecisionTable()
    if pass_name is not None:
        p = next((p for p in obj.passes if p.name == pass_name), None)
        if p is None:
            raise KeyError(f"decision {obj.name!r} has no pass {pass_name!r}")
        return p.table
    return obj.passes[0].table


def clauses_by_field(*tables: DecisionTable) -> dict[str, list[Clause]]:
    """Every clause referencing each field, across the given table(s)."""
    out: dict[str, list[Clause]] = {}
    for table in tables:
        for rule in table.rules:
            for c in rule.when:
                out.setdefault(c.field, []).append(c)
    return out


def _is_numeric_field(clauses: Sequence[Clause], type_hint: str | None) -> bool:
    if type_hint in ("int", "float"):
        return True
    if type_hint in ("enum", "str", "bool"):
        return False
    return any(c.op in NUMERIC_OPS for c in clauses)


def _numeric_thresholds(clauses: Sequence[Clause]) -> set[float]:
    ts: set[float] = set()
    for c in clauses:
        if c.op in NUMERIC_OPS or c.op in ("eq", "ne"):
            try:
                ts.add(float(c.value))
            except (TypeError, ValueError):
                continue
        elif c.op in SET_OPS:
            for v in c.value or []:
                try:
                    ts.add(float(v))
                except (TypeError, ValueError):
                    continue
    return ts


def _numeric_candidates(clauses: Sequence[Clause]) -> list[Any]:
    ts = sorted(_numeric_thresholds(clauses))
    if not ts:
        return [0.0, None]
    pts: list[Any] = [ts[0] - 1.0]
    for i, t in enumerate(ts):
        pts.append(float(t))
        if i + 1 < len(ts):
            pts.append((t + ts[i + 1]) / 2.0)
    pts.append(ts[-1] + 1.0)
    pts.append(None)  # null cell
    return pts


def _string_candidates(clauses: Sequence[Clause], domain: tuple[Any, ...] | None) -> list[Any]:
    vals: list[Any] = []
    for c in clauses:
        if c.op in ("eq", "ne"):
            vals.append(c.value)
        elif c.op in SET_OPS:
            vals.extend(c.value or [])
    if domain:
        vals.extend(domain)
    seen: list[Any] = []
    for v in vals:
        if v is not None and v not in seen:
            seen.append(v)
    return [*seen, _OTHER, None]


def _bool_candidates(clauses: Sequence[Clause]) -> list[Any]:
    cands: list[Any] = [True, False]
    if any(c.op in ("present", "absent", "is_null") for c in clauses):
        cands.append(None)
    return cands


def _signature(value: Any, clauses: Sequence[Clause]) -> tuple[bool, ...]:
    return tuple(match_scalar(c.op, value, c.value) for c in clauses)


def field_cells(
    fld: str, clauses: Sequence[Clause], type_hint: str | None = None,
    domain: tuple[Any, ...] | None = None,
) -> list[Cell]:
    """The distinguishable cells for ``fld`` — one representative per unique
    clause-truth signature (so values the table can't tell apart collapse)."""
    if type_hint == "bool":
        cands = _bool_candidates(clauses)
    elif _is_numeric_field(clauses, type_hint):
        cands = _numeric_candidates(clauses)
    else:
        cands = _string_candidates(clauses, domain)
    cells: list[Cell] = []
    seen: set[tuple[bool, ...]] = set()
    for v in cands:
        sig = _signature(v, clauses)
        if sig in seen:
            continue
        seen.add(sig)
        cells.append(Cell(fld, _cell_label(v), v, sig))
    return cells


def _cell_label(value: Any) -> str:
    if value is None:
        return "∅"
    if value == _OTHER:
        return "·other·"
    return str(value)


def build_partition(
    table: DecisionTable, catalog: Any | None = None, fields: Sequence[str] | None = None,
) -> dict[str, list[Cell]]:
    """Map each referenced field → its distinguishable cells."""
    by_field = clauses_by_field(table)
    if fields is not None:
        by_field = {f: by_field.get(f, []) for f in fields}
    out: dict[str, list[Cell]] = {}
    for fld, clauses in by_field.items():
        spec = catalog.fields.get(fld) if catalog else None
        out[fld] = field_cells(
            fld, clauses,
            type_hint=spec.type if spec else None,
            domain=spec.domain if spec else None,
        )
    return out


# ── Context enumeration & decision ────────────────────────────────────────────

@dataclass
class ContextRow:
    """One decision context: representative values per field, the table's outputs,
    and the dataset population that falls in this cell (0 unless weighted)."""

    context: dict[str, Any]
    labels: dict[str, str]
    outputs: dict[str, Any]
    key: tuple
    population: int = 0


@dataclass
class ContextTable:
    fields: list[str]
    outputs: list[str]
    rows: list[ContextRow]
    truncated: bool = False
    n_cells: dict[str, int] = field(default_factory=dict)

    def grouped(self) -> list[dict[str, Any]]:
        """Collapse rows by identical outputs → a compact decision summary."""
        groups: dict[tuple, dict[str, Any]] = {}
        for row in self.rows:
            okey = tuple(_norm_null(row.outputs.get(o)) for o in self.outputs)
            g = groups.setdefault(
                okey, {"outputs": dict(row.outputs), "contexts": 0, "population": 0,
                       "example": row.labels}
            )
            g["contexts"] += 1
            g["population"] += row.population
        return list(groups.values())


def _output_fields(table: DecisionTable, outputs: Sequence[str] | None) -> list[str]:
    cols = table.output_fields()
    if outputs is None:
        return cols
    return [c for c in cols if c in set(outputs)]


def _relevant_fields(table: DecisionTable, outputs: Sequence[str]) -> list[str]:
    """Fields referenced by rules that set any of ``outputs``."""
    want = set(outputs)
    fields: list[str] = []
    for rule in table.rules:
        if not (set(rule.set) & want):
            continue
        for c in rule.when:
            if c.field not in fields:
                fields.append(c.field)
    return fields


def unique_contexts(
    obj: Decision | DecisionTable,
    catalog: Any | None = None,
    *,
    pass_name: str | None = None,
    outputs: Sequence[str] | None = None,
    population: pd.DataFrame | None = None,
    max_contexts: int = 200_000,
) -> ContextTable:
    """Enumerate the table's exact, finite decision contexts and decide each.

    ``outputs`` restricts analysis to rules setting those columns (and only the
    fields they reference) — the recommended way to keep the cross-product small
    for multi-output tables (e.g. one stress direction at a time). ``population``
    (a materialized feature frame) weights each context by how many rows fall in
    it. Raises ``ValueError`` if the cross-product would exceed ``max_contexts``.
    """
    table = _table_of(obj, pass_name)
    out_cols = _output_fields(table, outputs)
    fields = _relevant_fields(table, out_cols) if outputs is not None else None
    partition = build_partition(table, catalog, fields)
    field_names = list(partition)

    total = 1
    for cells in partition.values():
        total *= max(1, len(cells))
    if total > max_contexts:
        raise ValueError(
            f"{total} contexts exceed max_contexts={max_contexts}; pass "
            f"outputs=[...] to analyze one output column at a time"
        )

    cell_lists = [partition[f] for f in field_names]
    combos = list(itertools.product(*cell_lists)) if cell_lists else [()]

    # Decide every context in one vectorized pass.
    data = {f: [combo[i].value for combo in combos] for i, f in enumerate(field_names)}
    frame = pd.DataFrame(data, index=range(len(combos))) if combos else pd.DataFrame()
    decided = table.apply(frame) if not frame.empty else frame

    rows: list[ContextRow] = []
    for r, combo in enumerate(combos):
        ctx = {field_names[i]: combo[i].value for i in range(len(field_names))}
        labels = {field_names[i]: combo[i].label for i in range(len(field_names))}
        key = tuple(combo[i].signature for i in range(len(field_names)))
        out = {c: (decided[c].iloc[r] if c in decided.columns else None) for c in out_cols}
        rows.append(ContextRow(context=ctx, labels=labels, outputs=out, key=key))

    ctable = ContextTable(
        fields=field_names, outputs=out_cols, rows=rows,
        n_cells={f: len(partition[f]) for f in field_names},
    )
    if population is not None:
        _weight(ctable, table, population)
    return ctable


def _weight(ctable: ContextTable, table: DecisionTable, population: pd.DataFrame) -> None:
    """Tally how many population rows fall in each context (by cell signature)."""
    by_field = clauses_by_field(table)
    by_key: dict[tuple, ContextRow] = {row.key: row for row in ctable.rows}
    for _, prow in population.iterrows():
        try:
            key = tuple(
                _signature(prow.get(f), by_field.get(f, [])) for f in ctable.fields
            )
        except Exception:
            continue
        match = by_key.get(key)
        if match is not None:
            match.population += 1


# ── Cross-engine diff & simulation ────────────────────────────────────────────

Decider = Callable[[pd.DataFrame], pd.DataFrame]


def decision_decider(obj: Decision | DecisionTable, pass_name: str | None = None) -> Decider:
    """A vectorized decider (``frame -> outputs frame``) for a table/decision."""
    table = _table_of(obj, pass_name)
    return lambda frame: table.apply(frame)


@dataclass
class DiffRow:
    labels: dict[str, str]
    a: dict[str, Any]
    b: dict[str, Any]
    population: int = 0


@dataclass
class EquivalenceReport:
    outputs: list[str]
    n_contexts: int
    diffs: list[DiffRow]

    @property
    def equivalent(self) -> bool:
        return not self.diffs


def cross_engine_diff(
    obj: Decision | DecisionTable,
    other: Decider,
    catalog: Any | None = None,
    *,
    pass_name: str | None = None,
    outputs: Sequence[str] | None = None,
    population: pd.DataFrame | None = None,
    max_contexts: int = 200_000,
) -> EquivalenceReport:
    """Decide every unique context in this engine *and* in ``other`` (e.g. a
    brokenspoke decider), then diff the small table → an equivalence report."""
    ctable = unique_contexts(
        obj, catalog, pass_name=pass_name, outputs=outputs,
        population=population, max_contexts=max_contexts,
    )
    frame = pd.DataFrame(
        {f: [row.context[f] for row in ctable.rows] for f in ctable.fields},
        index=range(len(ctable.rows)),
    )
    other_out = other(frame) if not frame.empty else frame
    diffs: list[DiffRow] = []
    for r, row in enumerate(ctable.rows):
        b = {c: (other_out[c].iloc[r] if c in other_out.columns else None) for c in ctable.outputs}
        if any(not _eq(row.outputs.get(c), b.get(c)) for c in ctable.outputs):
            diffs.append(DiffRow(labels=row.labels, a=dict(row.outputs), b=b,
                                 population=row.population))
    return EquivalenceReport(outputs=ctable.outputs, n_contexts=len(ctable.rows), diffs=diffs)


def simulate(
    before: Decision | DecisionTable,
    after: Decision | DecisionTable,
    catalog: Any | None = None,
    *,
    pass_name: str | None = None,
    outputs: Sequence[str] | None = None,
    population: pd.DataFrame | None = None,
    max_contexts: int = 200_000,
    variables: dict[str, Any] | None = None,
) -> EquivalenceReport:
    """Diff two rule versions over the union partition (changed-cells report).

    *variables* supplies values for ``$var:`` placeholders the rules read in their
    ``set:`` outputs (e.g. the stage-provided ``city_default_speed``). Absent a value,
    such a rule raises ``VarResolutionError``; with the variable present-but-None the
    null-injection guard simply skips it. Pass the city's stage variables to evaluate a
    ruleset as it would behave for that city.
    """
    ta = _table_of(before, pass_name)
    tb = _table_of(after, pass_name)
    combined = DecisionTable(rules=[*ta.rules, *tb.rules],
                             default={**(ta.default or {}), **(tb.default or {})} or None)
    out_cols = outputs if outputs is not None else sorted(
        set(ta.output_fields()) | set(tb.output_fields())
    )
    fields = _relevant_fields(combined, out_cols) if outputs is not None else None
    partition = build_partition(combined, catalog, fields)
    field_names = list(partition)

    total = 1
    for cells in partition.values():
        total *= max(1, len(cells))
    if total > max_contexts:
        raise ValueError(f"{total} contexts exceed max_contexts={max_contexts}")

    cell_lists = [partition[f] for f in field_names]
    combos = list(itertools.product(*cell_lists)) if cell_lists else [()]
    data = {f: [combo[i].value for combo in combos] for i, f in enumerate(field_names)}
    frame = pd.DataFrame(data, index=range(len(combos)))
    da = ta.apply(frame, variables=variables)
    db = tb.apply(frame, variables=variables)

    pop_by_key: dict[tuple, int] = {}
    if population is not None:
        by_field = clauses_by_field(combined)
        for _, prow in population.iterrows():
            key = tuple(_signature(prow.get(f), by_field.get(f, [])) for f in field_names)
            pop_by_key[key] = pop_by_key.get(key, 0) + 1

    diffs: list[DiffRow] = []
    for r, combo in enumerate(combos):
        a = {c: (da[c].iloc[r] if c in da.columns else None) for c in out_cols}
        b = {c: (db[c].iloc[r] if c in db.columns else None) for c in out_cols}
        if any(not _eq(a.get(c), b.get(c)) for c in out_cols):
            labels = {field_names[i]: combo[i].label for i in range(len(field_names))}
            key = tuple(combo[i].signature for i in range(len(field_names)))
            diffs.append(DiffRow(labels=labels, a=a, b=b, population=pop_by_key.get(key, 0)))
    return EquivalenceReport(outputs=list(out_cols), n_contexts=len(combos), diffs=diffs)


def _norm_null(v: Any) -> Any:
    """Map any null (None / NaN) to a single hashable sentinel for grouping."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return v


def _eq(a: Any, b: Any) -> bool:
    a_null = a is None or (isinstance(a, float) and np.isnan(a))
    b_null = b is None or (isinstance(b, float) and np.isnan(b))
    if a_null or b_null:
        return a_null and b_null
    return a == b
