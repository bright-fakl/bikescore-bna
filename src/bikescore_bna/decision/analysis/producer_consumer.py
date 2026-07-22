"""Resolve-time producer/consumer column validation (Phase 35i, M6).

The pipeline couples *producers* (attributes) to *consumers* (rulesets) **by
output-column name**: stress rules read ``speed_limit`` / ``ft_lanes`` / ``width_ft`` /
``ft_bike_infra``; segment/graph/neighborhood read ``one_way`` / ``functional_class`` /
``ft_park``. Nothing checks that a column a rule references is actually produced, so a
desync (e.g. a live rule referencing a column a frozen attribute no longer produces)
falls silently to a default or KeyErrors deep in a stage.

These helpers collect the **produced** and **referenced** column sets from a *resolved*
``BNAConfig`` so :meth:`BNAConfig.validate` can reject such a config at save/resolve.
The sets are computed from the resolved config, not a hard-coded list, so they stay
correct as attributes move (e.g. 35f folding imputation into attribute passes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only (avoid a config→decision cycle)
    from bikescore_bna.config import BNAConfig
    from bikescore_bna.decision.model import Decision


# Columns a rule may reference that a *stage* supplies at evaluation time rather than an
# attribute or ruleset producing them as ways_df columns:
#   functional_class — computed by the Attributes stage (also a functional_class ruleset
#                      output when that ruleset is active).
#   adj_fc           — adjacent-segment functional class; attached by the Stress stage
#                      before the stress rules run (a catalog ``frame`` field that is
#                      injected, not produced upstream).
_STAGE_INJECTED_COLUMNS = frozenset({"functional_class", "adj_fc"})

# Columns consumed directly by *stage code* (not via a ruleset), each tied to its
# consuming stage. They must be produced just like rule-referenced columns.
_STAGE_CONSUMED_COLUMNS: dict[str, str] = {
    "one_way": "Segment/Graph stage (one_way_car derivation, directed edges)",
    "functional_class": "Stress/Segment/Neighborhood stages",
    "ft_park": "Neighborhood stage (parking presence)",
}

# Rulesets whose *referenced* columns are checked against producers. Imputation moved
# into attribute fallback passes in Phase 35f (speed_limit / ft_lanes / tf_lanes), so it
# is no longer a standalone ruleset; attribute references self-validate at registration.
_CONSUMER_RULESETS = frozenset(
    {"functional_class", "stress_segment", "stress_intersection"}
)


def active_rulesets(config: BNAConfig) -> list[tuple[str, Decision]]:
    """Return ``(ruleset_name, Decision)`` for every non-``None`` ruleset in ``config``.

    Names use the CLI/web ruleset vocabulary (matching ``rules/catalogs.py``).
    """
    pairs = [
        ("stress_segment", config.stress.segment_rules),
        ("stress_intersection", config.stress.intersection_rules),
    ]
    return [(name, rs) for name, rs in pairs if rs is not None]


def _provider_fields() -> set[str]:
    """Every derived field any registered DSL provider can supply on demand.

    Provider fields (``adj_fc`` crossing booleans, ``access_ok``, ``footway_wide``,
    node-attribute lookups, …) are computed by the decision engine for the stage that
    needs them, so a rule may reference them without a column producer.
    """
    import bikescore_bna.rules.providers  # noqa: F401  (registers @register_provider)
    from bikescore_bna.decision.catalog import _PROVIDERS

    fields: set[str] = set()
    for provider in _PROVIDERS.values():
        fields |= set(provider.provides)
    return fields


def produced_fields(config: BNAConfig) -> set[str]:
    """Columns available to rules in a resolved ``config``.

    Union of: active attribute outputs, every active ruleset's output columns, the base
    OSM-tag columns, stage-injected columns, and provider-derived fields.
    """
    from bikescore_bna.stages.parse import BASE_WAY_TAGS

    produced: set[str] = set(BASE_WAY_TAGS)
    produced |= _STAGE_INJECTED_COLUMNS
    produced |= _provider_fields()
    if config.attributes is not None:
        for attr in config.attributes.in_topo_order():
            produced |= attr.output_columns()
    for _name, ruleset in active_rulesets(config):
        produced |= set(ruleset.output_fields())
    return produced


def unproduced_references(config: BNAConfig) -> list[tuple[str, str, str]]:
    """Find referenced columns that no producer supplies.

    Returns ``(column, source, rule_id)`` tuples — ``source`` is the consuming ruleset
    name or stage description, ``rule_id`` the offending rule's id (or
    ``"<stage consumer>"`` for hard-coded stage reads). Empty list ⇒ every consumer is
    satisfied. Only ``FieldRef``s are checked (``VarRef`` / ``$var:`` references are
    config variables, not columns — see :meth:`Rule.referenced_fields`).
    """
    produced = produced_fields(config)
    misses: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for name, decision in active_rulesets(config):
        if name not in _CONSUMER_RULESETS:
            continue
        for p in decision.passes:
            for rule in p.table.rules:
                for column in rule.referenced_fields():
                    if column not in produced:
                        key = (column, name, rule.id)
                        if key not in seen:
                            seen.add(key)
                            misses.append(key)
    for column, stage in _STAGE_CONSUMED_COLUMNS.items():
        if column not in produced:
            misses.append((column, stage, "<stage consumer>"))
    return misses
