"""Authoring layer — terse YAML → canonical model (design-review Appendix A.3–A.6).

Humans author decisions with a terse ``{field: matcher}`` grammar plus three
sugars; the loader normalizes everything to the canonical :mod:`model` form.
**Store canonical, render terse.**

Authoring features:

* **terse matchers** — scalar ⇒ ``eq``; list ⇒ ``in``; ``$name`` ⇒ ``in`` a named
  set; ``{op: value}`` ⇒ explicit op (two-bound ranges via ``{ge:.., le:..}``);
  ``present``/``absent``/``is_null`` ⇒ unary.
* **``sets:``** — named, reusable value collections referenced with ``$name``.
* **``predicates:``** + ``use:`` — named ``when``-fragments spliced (inlined at
  load, so analysis still sees flat clauses).
* **``for:`` sweep** + ``<var>`` placeholders — a rule block expanded once per
  value, ``<var>`` substituted. Placeholders live only in the authored layer; the
  canonical form has concrete names and zero placeholders. A ``for:`` may also sit on
  a nested rule block (cartesian with the enclosing sweep).
* **nested ``rules:``** — a scope-gated sub-tree: a ``when`` ANDs into every child
  rule, a ``when``-less child is the scope default. Compiles to flat first-match
  leaves (outer ``when`` + child ``when``), so analysis still sees flat clauses.

Authoring views (all compile to a :class:`DecisionTable`): rule table (``rules:``),
mapping (``map:``), resolution chain (``first_non_null:``). The loader rejects
``{var}`` placeholders and any unresolved ``<...>``.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from typing import Any

from bikescore.decision.model import (
    Clause,
    Decision,
    DecisionTable,
    FieldRef,
    LookupRef,
    Matcher,
    MatchRow,
    Pass,
    Rule,
    VarRef,
    _parse_enabled,
    _parse_set_value,
    _serialize_set_value,
)
from bikescore.decision.ops import OP_NAMES, UNARY_OPS

_UNARY_WORDS = frozenset({"present", "absent", "is_null"})
_CURLY = re.compile(r"\{[^}]*\}")
_PLACEHOLDER = re.compile(r"<[^>]+>")


# ── Value / matcher normalization ─────────────────────────────────────────────

def _resolve_value(value: Any, sets: Mapping[str, Any]) -> Any:
    """Resolve ``$var:NAME`` to a :class:`VarRef` and ``$name`` set references to
    their value list; pass other values through unchanged."""
    if isinstance(value, str) and value.startswith("$var:"):
        return VarRef(value[len("$var:"):])
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in sets:
            raise ValueError(f"unknown set reference ${name}")
        return list(sets[name])
    return value


def _normalize_matcher(field: str, matcher: Any, sets: Mapping[str, Any]) -> list[Clause]:
    """Compile one terse ``field: matcher`` entry into clauses (usually one)."""
    # Unary word: {aerialway: present}
    if isinstance(matcher, str) and matcher in _UNARY_WORDS:
        return [Clause(field, matcher)]
    # Variable reference: {speed_limit: $var:max_speed}  ⇒  eq
    if isinstance(matcher, str) and matcher.startswith("$var:"):
        return [Clause(field, "eq", _resolve_value(matcher, sets))]
    # Set reference: {adj_fc: $lesser}  ⇒  in
    if isinstance(matcher, str) and matcher.startswith("$"):
        return [Clause(field, "in", _resolve_value(matcher, sets))]
    # Explicit op(s): {not_in: $link_fcs} or {ge: 1, le: 25}
    if isinstance(matcher, dict):
        clauses: list[Clause] = []
        for op, val in matcher.items():
            if op not in OP_NAMES:
                raise ValueError(f"unknown operator {op!r} on field {field!r}")
            if op in UNARY_OPS:
                clauses.append(Clause(field, op))
            else:
                clauses.append(Clause(field, op, _resolve_value(val, sets)))
        return clauses
    # List ⇒ in
    if isinstance(matcher, (list, tuple)):
        return [Clause(field, "in", list(matcher))]
    # Scalar ⇒ eq
    return [Clause(field, "eq", matcher)]


def _normalize_when(
    when: Mapping[str, Any] | None,
    sets: Mapping[str, Any],
    predicates: Mapping[str, Any],
    _seen: frozenset[str] = frozenset(),
) -> list[Clause]:
    """Compile a terse ``when`` map into a flat clause list, splicing ``use:``."""
    if not when:
        return []
    clauses: list[Clause] = []
    for key, matcher in when.items():
        if key == "use":
            for pname in _as_list(matcher):
                if pname in _seen:
                    raise ValueError(f"recursive predicate {pname!r}")
                if pname not in predicates:
                    raise ValueError(f"unknown predicate {pname!r}")
                clauses.extend(
                    _normalize_when(predicates[pname], sets, predicates, _seen | {pname})
                )
            continue
        clauses.extend(_normalize_matcher(key, matcher, sets))
    return clauses


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


# ── Sweep expansion ───────────────────────────────────────────────────────────

def _substitute(obj: Any, mapping: Mapping[str, Any]) -> Any:
    """Replace every ``<var>`` placeholder in strings (keys and values)."""
    if isinstance(obj, str):
        out = obj
        for var, val in mapping.items():
            out = out.replace(f"<{var}>", str(val))
        return out
    if isinstance(obj, dict):
        return {_substitute(k, mapping): _substitute(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, mapping) for v in obj]
    return obj


def _sweep_iterations(for_spec: Mapping[str, Any] | list | None) -> list[dict[str, Any]]:
    """Expand a ``for:`` spec into a list of var→value bindings.

    Two authoring forms (both feed the pass-level and block-level sweeps):

    * **cartesian** (``dict``) — ``{a: [...], b: [...]}`` → the cartesian product of
      the per-variable value lists (the original behaviour).
    * **zip / paired** (``list`` of binding dicts, Phase 37) — ``[{a: 1, b: x},
      {a: 2, b: y}]`` is taken verbatim, one iteration per binding (lockstep). This
      expresses mirror-paired tables (e.g. ``ft→left`` / ``tf→right``) that the
      cartesian product cannot. Every binding must share the same key set.
    """
    if for_spec is None or for_spec == {}:
        return [{}]
    if isinstance(for_spec, list):                  # zip form (Phase 37): explicit bindings
        if not for_spec:                            # explicit empty paired sweep → zero iterations
            return []
        if not all(isinstance(b, Mapping) for b in for_spec):
            raise ValueError("a list-form `for:` must be a list of {var: value} bindings")
        keys = set().union(*(b.keys() for b in for_spec)) if for_spec else set()
        for b in for_spec:
            missing = keys - b.keys()
            if missing:
                raise ValueError(f"zip binding {dict(b)} missing vars {sorted(missing)}")
        return [dict(b) for b in for_spec]
    keys = list(for_spec)                            # cartesian form (unchanged)
    combos = itertools.product(*[for_spec[k] for k in keys])
    return [dict(zip(keys, combo)) for combo in combos]


# ── Rule / view compilation ───────────────────────────────────────────────────

def _check_no_placeholders(text: str, where: str) -> None:
    if _CURLY.search(text):
        raise ValueError(f"use <var> not {{var}} placeholders in {where}: {text!r}")
    if _PLACEHOLDER.search(text):
        raise ValueError(f"unresolved sweep placeholder in {where}: {text!r}")


def _compile_leaf(
    spec: Mapping[str, Any],
    rule_id: str,
    when: tuple[Clause, ...],
    vars: Mapping[str, Any] | None,
) -> Rule:
    """Compile a flat ``{when, set}`` leaf into a canonical :class:`Rule`.

    ``when`` is the fully-accumulated clause tuple (outer scope ANDed with the
    leaf's own ``when``); ``rule_id`` is the authored/derived base id (the sweep
    suffix and ``source`` provenance are applied here)."""
    raw_set = dict(spec.get("set", {}))
    for clause in when:
        _check_no_placeholders(clause.field, f"rule {rule_id!r} when field")
        if isinstance(clause.value, str):
            _check_no_placeholders(clause.value, f"rule {rule_id!r} when value")
    for k, v in raw_set.items():
        _check_no_placeholders(str(k), f"rule {rule_id!r} set key")
        if isinstance(v, str):
            _check_no_placeholders(v, f"rule {rule_id!r} set value")
    set_action = {k: _parse_set_value(v) for k, v in raw_set.items()}
    enabled = _parse_enabled(spec.get("enabled", True))
    # An explicit ``source`` (emitted by the ``decision_to_terse`` importer's flat
    # fallback for rules it could not re-collapse into a sweep) is carried verbatim so
    # the round-trip stays identity; authored docs never carry it (sweep-derived below).
    if "source" in spec:
        source: Mapping[str, Any] | None = dict(spec["source"])
    else:
        source = {"id": rule_id, "vars": dict(vars)} if vars else None
    out_id = rule_id
    if vars:
        out_id = rule_id + "__" + ",".join(f"{k}={v}" for k, v in vars.items())
    return Rule(id=out_id, when=when, set=set_action, source=source, enabled=enabled)


def _compile_rule_tree(
    spec: Mapping[str, Any],
    sets: Mapping[str, Any],
    predicates: Mapping[str, Any],
    vars: Mapping[str, Any] | None,
    parent_when: tuple[Clause, ...],
    parent_id: str,
    index: int,
) -> list[Rule]:
    """Compile one (possibly nested / block-swept) rule spec -> flat canonical rules.

    Three composing authoring constructs, all flattening to first-match leaves:

    * **block ``for:``** -- a sub-block sweep (cartesian with the enclosing sweep
      ``vars``); ``<var>`` is substituted into the block body before recursion and
      every emitted leaf carries the merged outer+block ``vars`` as ``source``
      provenance (so sweep analysis / re-collapse keep seeing it).
    * **nested ``rules:``** -- a scope-gated sub-tree: each descendant leaf's ``when``
      is the outer ``when`` ANDed with the child ``when`` in document order; a
      ``when``-less child is the scope default. A spec with ``rules:`` must not also
      carry ``set:``.
    * **leaf** -- a flat ``{when, set}`` rule.

    Ids are the spec's explicit ``id`` when present, else derived deterministically
    as ``f"{parent_id}_{index}"`` (stable for provenance/debug). Placeholder checks
    run on the flattened leaves.
    """
    if "for" in spec:
        block_for = spec["for"]
        body = {k: v for k, v in spec.items() if k != "for"}
        out: list[Rule] = []
        for combo in _sweep_iterations(block_for):
            merged = {**(vars or {}), **combo}
            out += _compile_rule_tree(
                _substitute(body, combo), sets, predicates, merged,
                parent_when, parent_id, index,
            )
        return out

    if "rules" in spec and "set" in spec:
        raise ValueError(
            f"rule {spec.get('id')!r} has both 'set' and 'rules' -- a scope-gated "
            f"parent carries only 'when' + child 'rules'"
        )

    node_id = str(spec["id"]) if "id" in spec else f"{parent_id}_{index}"
    when = parent_when + tuple(_normalize_when(spec.get("when"), sets, predicates))

    if "rules" in spec:
        out = []
        for i, child in enumerate(spec["rules"]):
            out += _compile_rule_tree(child, sets, predicates, vars, when, node_id, i)
        return out

    return [_compile_leaf(spec, node_id, when, vars)]


def _compile_map(
    spec: Mapping[str, Any],
    sets: Mapping[str, Any],
    predicates: Mapping[str, Any],
) -> tuple[list[Rule], dict[str, Any]]:
    """Compile a ``map:`` base layer → lowest-priority rules + a default.

    ``when:`` (optional) is a common gate ANDed into every generated map row.
    """
    src = spec["field"]
    to = spec["to"]
    gate = _normalize_when(spec.get("when"), sets, predicates)
    rules: list[Rule] = []
    for key, val in spec.get("table", {}).items():
        when = (Clause(src, "eq", key), *gate)
        rules.append(Rule(id=f"map_{to}_{key}", when=when, set={to: val}))
    default: dict[str, Any] = {}
    if "default" in spec:
        default[to] = spec["default"]
    return rules, default


def _compile_chain(
    output: str,
    chain: list[Any],
    sets: Mapping[str, Any],
    predicates: Mapping[str, Any],
) -> list[Rule]:
    """Compile a ``first_non_null`` resolution chain → first-match rules.

    Entries: a bare field name (use that field's value when present); a
    ``{value, when}`` literal; a ``{lookup, key}`` table lookup keyed on a field.
    """
    rules: list[Rule] = []
    for i, entry in enumerate(chain):
        rid = f"{output}_chain_{i}"
        if isinstance(entry, str):
            rules.append(Rule(id=rid, when=(Clause(entry, "present"),), set={output: FieldRef(entry)}))
        elif isinstance(entry, dict) and "value" in entry:
            when = tuple(_normalize_when(entry.get("when"), sets, predicates))
            rules.append(Rule(id=rid, when=when, set={output: _parse_set_value(entry["value"])}))
        elif isinstance(entry, dict) and "lookup" in entry:
            key = entry["key"]
            rules.append(
                Rule(id=rid, when=(Clause(key, "present"),),
                     set={output: LookupRef(lookup=entry["lookup"], key=key)})
            )
        else:
            raise ValueError(f"invalid first_non_null entry: {entry!r}")
    return rules


def _build_pass(
    spec: Mapping[str, Any],
    sets: Mapping[str, Any],
    predicates: Mapping[str, Any],
) -> Pass:
    name = spec["name"]
    after = spec.get("after")
    iterations = _sweep_iterations(spec.get("for"))

    rules: list[Rule] = []
    default: dict[str, Any] = {}
    for vars in iterations:
        block = _substitute(dict(spec), vars) if vars else dict(spec)
        # rule table (flat, nested scope-gated, or block-swept rules all flatten here)
        for i, rspec in enumerate(block.get("rules", [])):
            rules.extend(
                _compile_rule_tree(rspec, sets, predicates, vars or None, (), name, i)
            )
        # resolution chain: {first_non_null: [...]} keyed by the pass output (name)
        if "first_non_null" in block:
            rules.extend(_compile_chain(name, block["first_non_null"], sets, predicates))
        # default action (may be swept)
        if "default" in block and not _is_map_default(block):
            for k, v in block["default"].items():
                default[k] = v
    # mapping base layer (lowest priority) appended after override rules
    if "map" in spec:
        map_rules, map_default = _compile_map(spec["map"], sets, predicates)
        rules.extend(map_rules)
        default.update(map_default)
    return Pass(name=name, table=DecisionTable(rules=rules, default=default or None), after=after)


def _is_map_default(block: Mapping[str, Any]) -> bool:
    # A bare ``default:`` under a ``map:`` belongs to the map; handled separately.
    return "map" in block and "rules" not in block and "first_non_null" not in block


def _topo_passes(passes: list[Pass]) -> list[Pass]:
    """Order passes so each ``after`` target precedes it (stable on ties)."""
    by_name = {p.name: p for p in passes}
    ordered: list[Pass] = []
    visiting: set[str] = set()
    placed: set[str] = set()

    def visit(p: Pass) -> None:
        if p.name in placed:
            return
        if p.name in visiting:
            raise ValueError(f"cyclic pass ordering at {p.name!r}")
        visiting.add(p.name)
        if p.after:
            if p.after not in by_name:
                raise ValueError(f"pass {p.name!r} after unknown pass {p.after!r}")
            visit(by_name[p.after])
        visiting.discard(p.name)
        placed.add(p.name)
        ordered.append(p)

    for p in passes:
        visit(p)
    return ordered


# ── Public entry points ───────────────────────────────────────────────────────

def load_decision(data: Mapping[str, Any], name: str | None = None) -> Decision:
    """Compile an authored decision document into a canonical :class:`Decision`."""
    sets = data.get("sets", {})
    predicates = data.get("predicates", {})
    pass_specs = data.get("passes")
    if pass_specs is None:
        # single-pass shorthand: the document itself is the pass body
        pass_specs = [{"name": name or data.get("name", "decision"), **{
            k: v for k, v in data.items() if k not in ("sets", "predicates", "name")
        }}]
    passes = [_build_pass(p, sets, predicates) for p in pass_specs]
    passes = _topo_passes(passes)
    return Decision(name=name or data.get("name", "decision"), passes=passes)


def is_canonical_decision(data: object) -> bool:
    """True when ``data`` is a *canonical* Decision dict — each pass carries a
    ``table`` (the form ``Decision.to_dict``/``to_yaml`` emits).

    The terse authored / round-tripped form has passes carrying ``rules``/``for``
    and (optionally) top-level ``sets:``; it is compiled by :func:`load_decision`.
    Use this to choose the right loader for a stored or posted decision document.
    """
    if not isinstance(data, dict):
        return False
    passes = data.get("passes")
    return (
        isinstance(passes, list)
        and len(passes) > 0
        and all(isinstance(p, dict) and "table" in p for p in passes)
    )


def decision_from_doc(data: object) -> Decision:
    """Compile a stored/posted decision document, canonical **or** terse."""
    if is_canonical_decision(data):
        return Decision.from_dict(data)  # type: ignore[arg-type]
    return load_decision(data if isinstance(data, dict) else {"rules": data})


def load_matcher(data: Mapping[str, Any] | list | None, sets: Mapping[str, Any] | None = None) -> Matcher:
    """Compile an ``any:`` matcher document into a canonical :class:`Matcher`."""
    if not data:
        return Matcher(())
    sets = sets or {}
    rows_spec = data.get("any", data) if isinstance(data, dict) else data
    rows: list[MatchRow] = []
    for row in rows_spec or []:
        clauses = _normalize_when(row, sets, {})
        rows.append(MatchRow(clauses=tuple(clauses)))
    return Matcher(rows=tuple(rows))


# ── Importer: render canonical → terse (Phase 36a) ────────────────────────────
#
# The authored terse document is now the persisted, editable source of truth
# (stored verbatim, compiled on demand via ``decision_from_doc``). ``decision_to_terse``
# is demoted to an **importer**: it bootstraps an initial terse form only for a decision
# that arrives *canonical with no authored doc* (a code-built ``Decision``, a legacy
# snapshot, an external import). It is no longer on the save / normal-load / serialize
# paths for authored decisions.
#
# It is the inverse of the authoring compile: terse clauses, re-collapsed ``for:``
# sweeps (via the retained ``source:{id,vars}`` provenance), and re-extracted ``sets:``.
# Because ``canonical → terse`` is many-to-one it cannot reproduce an arbitrary authored
# doc (set names, nesting), which is exactly why authored docs are now stored verbatim.
# Every branch is still *verified* — a collapse/extraction that would not recompile to
# the identical canonical is dropped in favour of the flat form, so
# ``load_decision(decision_to_terse(d))`` is always ``d``.

def _clause_to_terse(clause: Clause) -> tuple[str, Any]:
    value = clause.value
    if isinstance(value, VarRef):
        value = f"$var:{value.name}"
    if clause.op == "eq":
        return clause.field, value
    if clause.op == "in":
        return clause.field, list(clause.value)
    if clause.op in UNARY_OPS:
        return clause.field, clause.op
    return clause.field, {clause.op: value}


def rule_to_terse(rule: Rule) -> dict:
    when: dict[str, Any] = {}
    for clause in rule.when:
        field, matcher = _clause_to_terse(clause)
        if field in when and isinstance(when[field], dict) and isinstance(matcher, dict):
            when[field].update(matcher)
        else:
            when[field] = matcher
    out: dict[str, Any] = {
        "id": rule.id,
        "when": when,
        "set": {k: _serialize_set_value(v) for k, v in rule.set.items()},
    }
    if rule.enabled is not True:
        from bikescore.decision.model import _serialize_enabled
        out["enabled"] = _serialize_enabled(rule.enabled)
    return out


def _terse_with_source(rule: Rule) -> dict:
    """``rule_to_terse`` plus the sweep ``source`` provenance, for the importer's flat
    fallback — so a decision whose rules cannot be re-collapsed into a ``for:`` sweep
    still round-trips ``load_decision(decision_to_terse(d)) == d`` (the ``source`` is
    honoured by :func:`_compile_leaf`)."""
    out = rule_to_terse(rule)
    if rule.source is not None:
        out["source"] = dict(rule.source)
    return out


def _unsubstitute(obj: Any, vars: Mapping[str, Any]) -> Any:
    """Reverse :func:`_substitute`: rewrite each concrete sweep value back to its
    ``<var>`` placeholder in every string (keys and values).  Longer values are
    replaced first so one value cannot clobber a prefix of another."""
    if isinstance(obj, str):
        out = obj
        for var, val in sorted(vars.items(), key=lambda kv: -len(str(kv[1]))):
            out = out.replace(str(val), f"<{var}>")
        return out
    if isinstance(obj, dict):
        return {_unsubstitute(k, vars): _unsubstitute(v, vars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unsubstitute(v, vars) for v in obj]
    return obj


def _collapse_pass(p: Pass) -> tuple[dict | None, list[dict]]:
    """Re-collapse a ``for:`` sweep from rule ``source`` provenance.

    Returns ``(for_spec, authored_rules)`` when every rule in the pass is a clean
    sweep expansion that re-compiles bit-for-bit, else ``(None, flat_rules)``.
    """
    rules = p.table.rules
    flat = [_terse_with_source(r) for r in rules]
    if not rules or any(r.source is None for r in rules):
        return None, flat
    # Iteration order = distinct ``source.vars`` by first appearance.
    iterations: list[dict] = []
    for r in rules:
        v = dict(r.source["vars"])  # type: ignore[index]
        if v not in iterations:
            iterations.append(v)
    if len(iterations) < 2:
        return None, flat
    keys = list(iterations[0])
    if any(list(it) != keys for it in iterations):
        return None, flat
    for_spec: dict[str, list] = {k: [] for k in keys}
    for it in iterations:
        for k in keys:
            if it[k] not in for_spec[k]:
                for_spec[k].append(it[k])
    first = iterations[0]
    authored_ids: list[str] = []
    for r in rules:
        if dict(r.source["vars"]) == first:  # type: ignore[index]
            authored_ids.append(str(r.source["id"]))  # type: ignore[index]
    authored: list[dict] = []
    for aid in authored_ids:
        inst = next(
            r for r in rules
            if str(r.source["id"]) == aid and dict(r.source["vars"]) == first  # type: ignore[index]
        )
        terse = rule_to_terse(inst)
        authored.append({
            "id": aid,
            "when": _unsubstitute(terse["when"], first),
            "set": _unsubstitute(terse["set"], first),
        })
    candidate: dict[str, Any] = {"name": p.name, "for": for_spec, "rules": authored}
    if p.table.default:
        candidate["default"] = dict(p.table.default)
    try:
        rebuilt = _build_pass(candidate, {}, {})
    except Exception:
        return None, flat
    if rebuilt.table.rules != rules:
        return None, flat
    if (rebuilt.table.default or None) != (p.table.default or None):
        return None, flat
    return for_spec, authored


def _extract_sets(doc: dict) -> dict:
    """Hoist list-valued ``in`` matchers used 2+ times into named ``sets:``."""
    import copy
    from collections import Counter

    counts: Counter = Counter()
    for p in doc["passes"]:
        for r in p.get("rules", []):
            for v in r.get("when", {}).values():
                if isinstance(v, list):
                    try:
                        counts[tuple(v)] += 1
                    except TypeError:
                        pass
    repeated = [t for t, n in counts.items() if n >= 2]
    if not repeated:
        return doc
    names = {
        t: f"set_{i + 1}"
        for i, t in enumerate(sorted(repeated, key=lambda x: (-len(x), x)))
    }
    new = copy.deepcopy(doc)
    for p in new["passes"]:
        for r in p.get("rules", []):
            when = r.get("when", {})
            for k, v in list(when.items()):
                if isinstance(v, list) and tuple(v) in names:
                    when[k] = "$" + names[tuple(v)]
    new = {"name": new.get("name"), "sets": {n: list(t) for t, n in names.items()},
           **{k: v for k, v in new.items() if k != "name"}}
    return new


def decision_to_terse(decision: Decision) -> dict:
    """Importer (Phase 36a): bootstrap a terse doc from a canonical ``Decision``.

    Used only when no authored terse doc was retained (code-built decisions, legacy
    snapshots, external imports). Authored decisions are stored + served verbatim; this
    is not on the save/normal-load/serialize paths for them.
    """
    passes: list[dict] = []
    for p in decision.passes:
        block: dict[str, Any] = {"name": p.name}
        if p.after:
            block["after"] = p.after
        for_spec, rule_dicts = _collapse_pass(p)
        if for_spec:
            block["for"] = for_spec
        block["rules"] = rule_dicts
        if p.table.default:
            block["default"] = dict(p.table.default)
        passes.append(block)
    out: dict[str, Any] = {"name": decision.name, "passes": passes}
    with_sets = _extract_sets(out)
    if load_decision(with_sets).to_dict() == decision.to_dict():
        return with_sets
    return out
