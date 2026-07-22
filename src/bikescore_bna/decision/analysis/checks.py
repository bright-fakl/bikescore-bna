"""Static validation + shadowing / exhaustiveness (design-review A.7).

Two families of checks, both impossible on an expression-string DSL and enabled
here by the structured grammar + typed catalog:

* **Static validation** — unknown field references, values outside an enum field's
  domain, op/type mismatches (a numeric op on a string field), duplicate rule ids,
  unresolved ``<…>`` / ``{…}`` placeholders.
* **Shadowing / unreachable / exhaustiveness** — subsumption reasoning over the
  typed domains: a rule is dead if an earlier rule's clauses subsume it; a table is
  non-exhaustive (missing default) if the cell partition leaves cells uncovered.
  Subset/superset checks first; full satisfiability is a later increment.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from bikescore_bna.decision.analysis.contexts import unique_contexts
from bikescore_bna.decision.model import (
    Clause,
    Decision,
    DecisionTable,
    Rule,
    VarRef,
)
from bikescore_bna.decision.ops import NUMERIC_OPS, SET_OPS

_PLACEHOLDER = re.compile(r"<[^>]+>|\{[^}]*\}")


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    rule_id: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.rule_id}]" if self.rule_id else ""
        return f"{self.severity.upper()} {self.code}{loc}: {self.message}"


# ── Static validation ─────────────────────────────────────────────────────────

def _has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER.search(value))


def validate_decision(decision: Decision, catalog: Any | None = None) -> list[Issue]:
    """Static checks over a compiled decision (+ optional typed catalog)."""
    issues: list[Issue] = []
    seen_ids: set[str] = set()
    for p in decision.passes:
        for rule in p.table.rules:
            if rule.id in seen_ids:
                issues.append(Issue("error", "duplicate-id",
                                    f"duplicate rule id {rule.id!r}", rule.id))
            seen_ids.add(rule.id)
            for c in rule.when:
                issues.extend(_check_clause(c, rule.id, catalog))
            for k, v in rule.set.items():
                if _has_placeholder(k) or _has_placeholder(v):
                    issues.append(Issue("error", "placeholder",
                                        f"unresolved placeholder in set {k!r}", rule.id))
                if isinstance(v, VarRef):
                    issues.append(Issue("info", "var-ref",
                                        f"set {k!r} uses variable {v.name!r} (may be skipped at runtime)",
                                        rule.id))
    return issues


def _check_clause(c: Clause, rule_id: str, catalog: Any | None) -> list[Issue]:
    issues: list[Issue] = []
    if _has_placeholder(c.field):
        issues.append(Issue("error", "placeholder",
                            f"unresolved placeholder in field {c.field!r}", rule_id))
    if _has_placeholder(c.value):
        issues.append(Issue("error", "placeholder",
                            f"unresolved placeholder in value {c.value!r}", rule_id))
    if catalog is None:
        return issues
    spec = catalog.fields.get(c.field)
    if spec is None:
        issues.append(Issue("warning", "unknown-field",
                            f"field {c.field!r} not in catalog {catalog.name!r}", rule_id))
        return issues
    if c.op in NUMERIC_OPS and spec.type not in ("int", "float"):
        issues.append(Issue("error", "op-type",
                            f"numeric op {c.op!r} on {spec.type} field {c.field!r}", rule_id))
    if spec.type == "enum" and spec.domain:
        vals = c.value if c.op in SET_OPS else [c.value]
        for v in (vals or []):
            if v is not None and v not in spec.domain:
                issues.append(Issue("warning", "enum-domain",
                                    f"value {v!r} not in domain of enum {c.field!r}", rule_id))
    return issues


# ── Subsumption / shadowing ───────────────────────────────────────────────────

def _clauses_on(rule: Rule, fld: str) -> list[Clause]:
    return [c for c in rule.when if c.field == fld]


def _implies(j_clauses: Sequence[Clause], target: Clause) -> bool:
    """Does the AND of ``j_clauses`` (all on ``target.field``) guarantee ``target``?

    Conservative: returns True only when the implication is certain (so shadowing
    is never flagged spuriously)."""
    op, val = target.op, target.value
    eqs = [c.value for c in j_clauses if c.op == "eq"]
    ins = [set(_as_list(c.value)) for c in j_clauses if c.op == "in"]
    j_vals: set | None = None
    if eqs:
        j_vals = {eqs[0]}
    elif ins:
        j_vals = set.intersection(*ins) if len(ins) > 1 else ins[0]

    if op == "present":
        return bool(eqs) or bool(ins) or any(
            c.op in NUMERIC_OPS or c.op == "present" for c in j_clauses
        )
    if op in ("absent", "is_null"):
        return any(c.op in ("absent", "is_null") for c in j_clauses)
    if op == "eq":
        return j_vals is not None and j_vals <= {val}
    if op == "in":
        return j_vals is not None and j_vals <= set(_as_list(val))
    if op == "ne":
        if j_vals is not None:
            return val not in j_vals
        return any(c.op == "ne" and c.value == val for c in j_clauses)
    if op == "not_in":
        target_set = set(_as_list(val))
        return j_vals is not None and not (j_vals & target_set)
    if op in NUMERIC_OPS:
        return _numeric_implies(j_clauses, op, val)
    return False


def _numeric_implies(j_clauses: Sequence[Clause], op: str, val: Any) -> bool:
    try:
        t = float(val)
    except (TypeError, ValueError):
        return False
    eqs = [float(c.value) for c in j_clauses if c.op == "eq"]
    if eqs:
        v = eqs[0]
        return {"le": v <= t, "lt": v < t, "ge": v >= t, "gt": v > t}[op]
    ups = [float(c.value) for c in j_clauses if c.op == "le"]
    ups_strict = [float(c.value) for c in j_clauses if c.op == "lt"]
    los = [float(c.value) for c in j_clauses if c.op == "ge"]
    los_strict = [float(c.value) for c in j_clauses if c.op == "gt"]
    if op == "le":
        return any(u <= t for u in ups) or any(u <= t for u in ups_strict)
    if op == "lt":
        return any(u < t for u in ups) or any(u <= t for u in ups_strict)
    if op == "ge":
        return any(lo >= t for lo in los) or any(lo >= t for lo in los_strict)
    if op == "gt":
        return any(lo > t for lo in los) or any(lo >= t for lo in los_strict)
    return False


def _subsumes(outer: Rule, inner: Rule) -> bool:
    """region(inner) ⊆ region(outer): every clause of ``outer`` is implied by
    ``inner``'s clauses on the same field (an unconstrained field can't be)."""
    for oc in outer.when:
        if not _implies(_clauses_on(inner, oc.field), oc):
            return False
    return True


def find_unreachable(obj: Decision | DecisionTable) -> list[Issue]:
    """Rules that can never win — each output column they set is subsumed by an
    earlier rule that also sets it."""
    tables = obj.passes if isinstance(obj, Decision) else None
    rule_tables = [(p.name, p.table) for p in tables] if tables else [("", obj)]  # type: ignore[list-item]
    issues: list[Issue] = []
    for _name, table in rule_tables:
        for j, rule in enumerate(table.rules):
            if not rule.set:
                continue
            shadowed_all = True
            blocker: str | None = None
            for col in rule.set:
                earlier = [r for r in table.rules[:j] if col in r.set]
                # A covering rule whose set value is a VarRef may be skipped at
                # runtime (null-injection guard), so it cannot reliably shadow.
                cover = next(
                    (r for r in earlier
                     if _subsumes(r, rule) and not isinstance(r.set.get(col), VarRef)),
                    None,
                )
                if cover is None:
                    shadowed_all = False
                    break
                blocker = cover.id
            if shadowed_all and blocker is not None:
                issues.append(Issue("warning", "unreachable",
                                    f"rule {rule.id!r} is shadowed by {blocker!r}", rule.id))
    return issues


def check_exhaustive(
    obj: Decision | DecisionTable, catalog: Any | None = None, *,
    pass_name: str | None = None, outputs: Sequence[str] | None = None,
    max_contexts: int = 200_000,
) -> list[Issue]:
    """Warn when a table has no default for an output yet leaves cells uncovered
    (a feature could fall through to its prior/NaN value unintentionally)."""
    table = obj.passes[0].table if isinstance(obj, Decision) and pass_name is None else None
    if isinstance(obj, Decision):
        if pass_name is not None:
            table = next(p.table for p in obj.passes if p.name == pass_name)
        elif table is None:
            table = obj.passes[0].table
    else:
        table = obj
    out_cols = [c for c in table.output_fields()
                if outputs is None or c in set(outputs)]
    default = table.default or {}
    issues: list[Issue] = []
    ctable = unique_contexts(table, catalog, outputs=outputs, max_contexts=max_contexts)
    for col in out_cols:
        if col in default:
            continue
        uncovered = sum(
            1 for row in ctable.rows
            if row.outputs.get(col) is None
            or (isinstance(row.outputs.get(col), float) and row.outputs.get(col) != row.outputs.get(col))
        )
        if uncovered:
            issues.append(Issue("warning", "non-exhaustive",
                                f"output {col!r} has no default; {uncovered} "
                                f"context cell(s) uncovered"))
    return issues


def _as_list(value: Any) -> list:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]
