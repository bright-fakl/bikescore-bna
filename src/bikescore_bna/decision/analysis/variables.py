"""Resolve-time ``$var:`` variable validation (Phase 35h).

A rule or attribute may reference a config variable as ``$var:name``. Two failure
modes existed silently:

* **Undeclared** — ``name`` is neither in ``config.variables`` (user-defined, Phase
  34c) nor provided by a stage (``BaseStage._RULE_VARIABLES``, Phase 31f). Set-side this
  resolved to ``None`` and *skipped the rule* (null-injection guard); predicate-side it
  ``VarResolutionError``ed deep in a stage. A typo became a silently-dropped rule.
* **Orphan override** — a *city* sets ``variables.X`` that no active rule/attribute
  references, so the override does nothing.

These helpers collect referenced/declared variable sets from a *resolved* ``BNAConfig``
so :meth:`BNAConfig.validate` can reject an undeclared reference at save/resolve, and so
the web editors can surface the offer-to-declare and orphan-override affordances.

Design notes:

* A scenario must declare every ``$var:`` its own rules/attributes reference — scenarios
  are self-contained units (Phase 35 supplement M1/M2); a scenario may not depend on a
  variable that only a city defines. Hence the undeclared check runs against the
  *scenario's own* ``config.variables`` (city-free resolution), plus the fixed
  stage-provided vocabulary.
* Stage-declared variables (``_RULE_VARIABLES``: ``city_default_speed`` → ``city.default_speed``,
  …) are part of the foundation contract, always available, and are whitelisted — the
  analogue of ``producer_consumer._provider_fields`` for columns.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from bikescore_bna.decision.analysis.producer_consumer import active_rulesets

if TYPE_CHECKING:  # pragma: no cover - typing only (avoid a config→decision cycle)
    from bikescore_bna.config import BNAConfig


def stage_declared_variables() -> set[str]:
    """Every ``$var:`` name a pipeline stage injects via ``_RULE_VARIABLES``.

    These map dotted config paths into the rule/attribute ``variables`` dict at runtime
    (``build_rule_variables``); they are the foundation's fixed variable vocabulary, so a
    reference to one is always declared regardless of the scenario.

    Core has no runtime stage registry (the DAG lives in the orchestration app), so the
    vocabulary is gathered from the module that defines each ``PIPELINE`` stage: a stage
    keeps its ``_RULE_VARIABLES`` as a module-level dict, and ``spec.run.__module__``
    points at it. Walking ``PIPELINE`` auto-discovers any stage's variables without a
    hard-coded module list.
    """
    import importlib

    from bikescore_bna.pipeline import PIPELINE

    names: set[str] = set()
    seen: set[str] = set()
    for spec in PIPELINE:
        module_name = getattr(spec.run, "__module__", None)
        if not module_name or module_name in seen:
            continue
        seen.add(module_name)
        module = importlib.import_module(module_name)
        rule_variables = getattr(module, "_RULE_VARIABLES", None)
        if isinstance(rule_variables, dict):
            names |= set(rule_variables.keys())
    return names


def variable_references(config: BNAConfig) -> dict[str, list[str]]:
    """``$var:`` name → sorted sources that reference it (rules + attributes).

    Sources read like ``"stress_segment rule 'speed_high'"`` or ``"attribute
    'speed_parsed'"`` for a per-variable "referenced-by" view.
    """
    refs: dict[str, set[str]] = {}
    for name, decision in active_rulesets(config):
        for p in decision.passes:
            for rule in p.table.rules:
                for var in rule.referenced_variables():
                    refs.setdefault(var, set()).add(f"{name} rule {rule.id!r}")
    if config.attributes is not None:
        for attr in config.attributes.in_topo_order():
            for var in attr.referenced_variables():
                refs.setdefault(var, set()).add(f"attribute {attr.name!r}")
    return {k: sorted(v) for k, v in sorted(refs.items())}


def undeclared_variables(config: BNAConfig) -> dict[str, list[str]]:
    """Referenced ``$var:`` names that are neither declared nor stage-provided.

    Returns ``{name: [sources]}`` — empty ⇒ every reference resolves. ``name`` counts as
    declared if it is a key of ``config.variables`` or a stage-injected variable.
    """
    declared = set(config.variables or {}) | stage_declared_variables()
    return {
        name: srcs
        for name, srcs in variable_references(config).items()
        if name not in declared
    }


def null_variables(config: BNAConfig) -> dict[str, list[str]]:
    """Referenced, user-declared variables whose resolved value is ``None``.

    Advisory only: a null variable skips its rule (the documented null-injection guard),
    which is sometimes intentional (an optional knob) and sometimes a city that has not
    filled a locale fact. Stage-declared vars are excluded — their null is the intended
    fallback-cascade tier (e.g. a city with no ``default_speed``).
    """
    refs = variable_references(config)
    declared = config.variables or {}
    return {
        name: srcs
        for name, srcs in refs.items()
        if name in declared and declared.get(name) is None
    }


def orphan_overrides(
    config: BNAConfig, override_variable_names: Iterable[str]
) -> list[str]:
    """City ``variables`` overrides that no active rule/attribute references.

    Such an override has no effect — the scenario does not consume it. ``config`` is the
    city's *resolved* config; ``override_variable_names`` the keys the city set under its
    ``variables`` override layer.
    """
    referenced = set(variable_references(config))
    return sorted(name for name in override_variable_names if name not in referenced)
