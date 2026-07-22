"""Decision-DSL analysis surface (``bikescore_bna.decision.analysis``).

Ported from bna-core's Phase 30e/35h/35i suites when the analysis package was moved
into the core library (spec-issue OPEN-38k-decision-analysis). Covers trace, coverage /
never-fired, exact threshold-partitioned unique contexts, unreachable / exhaustiveness /
static validation, the cross-engine diff harness, simulation, and the resolve-time
producer/consumer + ``$var:`` checks that ``BNAConfig.validate`` depends on.

Library-level only: the ``bikescore-bna rules`` analysis CLI lives in the orchestration app,
not core, so its tests stay there.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bikescore_bna.config import BNAConfig
from bikescore_bna.decision import Decision, FieldCatalog, FieldSpec, load_decision
from bikescore_bna.decision.analysis import (
    check_exhaustive,
    coverage,
    cross_engine_diff,
    decision_decider,
    field_cells,
    find_unreachable,
    simulate,
    trace,
    unique_contexts,
    validate_decision,
)
from bikescore_bna.decision.analysis.contexts import clauses_by_field
from bikescore_bna.decision.analysis.producer_consumer import (
    produced_fields,
    unproduced_references,
)
from bikescore_bna.decision.analysis.variables import (
    null_variables,
    orphan_overrides,
    stage_declared_variables,
    undeclared_variables,
    variable_references,
)
from bikescore_bna.rules import (
    SEGMENT_CATALOG,
    catalog_for,
    default_intersection_stress_rules,
    default_segment_stress_rules,
)

# ── A tiny self-contained decision used by the precise tests ──────────────────

def _toy() -> Decision:
    return load_decision({
        "name": "toy",
        "sets": {"low_fc": ["residential", "unclassified"]},
        "passes": [{
            "name": "stress",
            "rules": [
                {"id": "high_speed", "when": {"fc": "$low_fc", "speed": {"gt": 25}},
                 "set": {"stress": 3}},
                {"id": "low_speed", "when": {"fc": "$low_fc", "speed": {"le": 25}},
                 "set": {"stress": 1}},
            ],
            "default": {"stress": 2},
        }],
    })


def _toy_catalog() -> FieldCatalog:
    cat = FieldCatalog("toy")
    cat.frame("fc", "enum").frame("speed", "int")
    return cat


# ── Unique decision contexts: exact + finite over continuous fields ───────────

def test_contexts_continuous_field_binned_at_thresholds() -> None:
    ct = unique_contexts(_toy(), _toy_catalog())
    assert ct.n_cells["speed"] == 3  # {<=25, >25, ∅}
    assert ct.n_cells["fc"] == 2
    assert len(ct.rows) == 6


def test_contexts_decisions_exact() -> None:
    ct = unique_contexts(_toy(), _toy_catalog())
    groups = {tuple(g["outputs"].items()): g["contexts"] for g in ct.grouped()}
    assert groups[(("stress", 3),)] == 1
    assert groups[(("stress", 1),)] == 1
    assert groups[(("stress", 2),)] == 4


def test_field_cells_signature_collapse() -> None:
    clauses = clauses_by_field(_toy().passes[0].table)["fc"]
    cells = field_cells("fc", clauses, type_hint="enum")
    labels = {c.label for c in cells}
    assert "·other·" in labels and "residential" in labels
    assert len(cells) == 2


def test_contexts_population_weighting() -> None:
    df = pd.DataFrame({
        "fc": ["residential", "residential", "primary"],
        "speed": [30, 10, 40],
    })
    ct = unique_contexts(_toy(), _toy_catalog(), population=df)
    assert sum(r.population for r in ct.rows) == 3
    hi = next(r for r in ct.rows if r.outputs["stress"] == 3)
    assert hi.population == 1


def test_contexts_segment_per_output_is_compact() -> None:
    ct = unique_contexts(default_segment_stress_rules(), SEGMENT_CATALOG,
                         outputs=["ft_seg_stress"])
    assert ct.n_cells["adj_fc"] == 9
    outs = {g["outputs"]["ft_seg_stress"] for g in ct.grouped()}
    assert 1.0 in outs and 3.0 in outs


# ── Trace ─────────────────────────────────────────────────────────────────────

def test_trace_winning_rule_and_clause_outcomes() -> None:
    df = pd.DataFrame({"fc": ["residential"], "speed": [40]})
    ft = trace(_toy(), df, _toy_catalog(), index=0)
    win = next(r for r in ft.passes[0].rules if r.matched)
    assert win.id == "high_speed"
    assert ft.winning_rule("stress") == "high_speed"
    assert [(c.field, c.ok) for c in win.clauses] == [("fc", True), ("speed", True)]


def test_trace_default_path() -> None:
    df = pd.DataFrame({"fc": ["primary"], "speed": [40]})
    ft = trace(_toy(), df, _toy_catalog(), index=0)
    assert ft.passes[0].default_won == ["stress"]
    assert ft.passes[0].produced["stress"] == 2


def test_trace_segment_real_rule() -> None:
    df = pd.DataFrame({
        "adj_fc": ["motorway"], "speed_limit": [55], "ft_lanes": [2], "tf_lanes": [2],
        "ft_bike_infra": [None], "tf_bike_infra": [None],
        "ft_bike_infra_width": [np.nan], "tf_bike_infra_width": [np.nan], "bicycle": [None],
    })
    ft = trace(default_segment_stress_rules(), df, SEGMENT_CATALOG, index=0)
    assert ft.winning_rule("ft_seg_stress").startswith("motorway_trunk")


# ── Coverage / never-fired ────────────────────────────────────────────────────

def test_coverage_counts_and_never_fired() -> None:
    df = pd.DataFrame({
        "fc": ["residential", "residential", "primary"],
        "speed": [40, 10, 40],
    })
    report = coverage(_toy(), df, _toy_catalog())
    counts = report.counts()
    assert counts["high_speed"] == 1
    assert counts["low_speed"] == 1
    assert report.passes[0].columns[0].default_count == 1


def test_coverage_aggregates_sweep_to_authored_id() -> None:
    df = pd.DataFrame({
        "adj_fc": ["motorway"], "speed_limit": [55], "ft_lanes": [2], "tf_lanes": [2],
        "ft_bike_infra": [None], "tf_bike_infra": [None],
        "ft_bike_infra_width": [np.nan], "tf_bike_infra_width": [np.nan], "bicycle": [None],
    })
    report = coverage(default_segment_stress_rules(), df, SEGMENT_CATALOG)
    assert "motorway_trunk" in report.counts()
    assert not any("__dir=" in rid for rid in report.counts())


# ── Shadowing / unreachable / exhaustiveness ─────────────────────────────────

def test_unreachable_flagged() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p",
        "rules": [
            {"id": "broad", "when": {"fc": ["residential", "unclassified"]}, "set": {"x": 1}},
            {"id": "narrow", "when": {"fc": "residential"}, "set": {"x": 2}},
        ],
    }]})
    issues = find_unreachable(dec)
    assert any(i.code == "unreachable" and i.rule_id == "narrow" for i in issues)


def test_unreachable_numeric_subsumption() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p",
        "rules": [
            {"id": "broad", "when": {"speed": {"le": 30}}, "set": {"x": 1}},
            {"id": "narrow", "when": {"speed": {"le": 25}}, "set": {"x": 2}},
        ],
    }]})
    assert any(i.rule_id == "narrow" for i in find_unreachable(dec))


def test_independent_rules_not_flagged() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p",
        "rules": [
            {"id": "a", "when": {"fc": "residential"}, "set": {"x": 1}},
            {"id": "b", "when": {"fc": "primary"}, "set": {"x": 2}},
        ],
    }]})
    assert find_unreachable(dec) == []


def test_exhaustiveness_warns_missing_default() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p",
        "rules": [{"id": "a", "when": {"fc": "residential"}, "set": {"x": 1}}],
    }]})
    assert any(i.code == "non-exhaustive" for i in check_exhaustive(dec))


def test_exhaustiveness_silent_with_default() -> None:
    assert check_exhaustive(_toy(), _toy_catalog()) == []


# ── Static validation ─────────────────────────────────────────────────────────

def test_validate_unknown_field() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p", "rules": [{"id": "a", "when": {"nope": 1}, "set": {"x": 1}}]}]})
    assert any(i.code == "unknown-field" for i in validate_decision(dec, _toy_catalog()))


def test_validate_op_type_mismatch() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p", "rules": [{"id": "a", "when": {"fc": {"ge": 5}}, "set": {"x": 1}}]}]})
    issues = validate_decision(dec, _toy_catalog())
    assert any(i.code == "op-type" and i.severity == "error" for i in issues)


def test_validate_enum_domain() -> None:
    cat = FieldCatalog("c")
    cat.add(FieldSpec("fc", "frame", "enum", domain=("residential", "primary")))
    dec = load_decision({"name": "s", "passes": [{
        "name": "p", "rules": [{"id": "a", "when": {"fc": "bogus"}, "set": {"x": 1}}]}]})
    assert any(i.code == "enum-domain" for i in validate_decision(dec, cat))


def test_validate_duplicate_ids() -> None:
    dec = load_decision({"name": "s", "passes": [{
        "name": "p", "rules": [
            {"id": "dup", "when": {"fc": "a"}, "set": {"x": 1}},
            {"id": "dup", "when": {"fc": "b"}, "set": {"x": 2}},
        ]}]})
    assert any(i.code == "duplicate-id" for i in validate_decision(dec))


def test_validate_default_rulesets_clean() -> None:
    for loader, cat in [
        (default_segment_stress_rules, SEGMENT_CATALOG),
        (default_intersection_stress_rules, catalog_for("stress_intersection")),
    ]:
        errors = [i for i in validate_decision(loader(), cat) if i.severity == "error"]
        assert errors == [], errors


# ── Cross-engine diff harness ─────────────────────────────────────────────────

def test_cross_engine_identical_is_equivalent() -> None:
    seg = default_segment_stress_rules()
    rep = cross_engine_diff(seg, decision_decider(seg), SEGMENT_CATALOG,
                            outputs=["ft_seg_stress"])
    assert rep.equivalent
    assert rep.n_contexts > 0


def test_cross_engine_detects_difference() -> None:
    seg = default_segment_stress_rules()
    other = load_decision({"name": "x", "passes": [{
        "name": "seg_stress",
        "rules": [{"id": "all3", "when": {"adj_fc": "residential"}, "set": {"ft_seg_stress": 3}}],
        "default": {"ft_seg_stress": 1},
    }]})
    rep = cross_engine_diff(seg, decision_decider(other), SEGMENT_CATALOG,
                            outputs=["ft_seg_stress"])
    assert not rep.equivalent
    assert rep.diffs


# ── Simulation ────────────────────────────────────────────────────────────────

def test_simulate_changed_cells() -> None:
    before = _toy()
    after = load_decision({
        "name": "toy", "sets": {"low_fc": ["residential", "unclassified"]},
        "passes": [{
            "name": "stress",
            "rules": [
                {"id": "high_speed", "when": {"fc": "$low_fc", "speed": {"gt": 30}},
                 "set": {"stress": 3}},
                {"id": "low_speed", "when": {"fc": "$low_fc", "speed": {"le": 30}},
                 "set": {"stress": 1}},
            ],
            "default": {"stress": 2},
        }],
    })
    rep = simulate(before, after, _toy_catalog())
    assert rep.diffs
    d = rep.diffs[0]
    assert d.a["stress"] == 3 and d.b["stress"] == 1


def test_simulate_identical_no_change() -> None:
    seg = default_segment_stress_rules()
    assert simulate(seg, seg, SEGMENT_CATALOG, outputs=["ft_seg_stress"]).diffs == []


# ── Producer/consumer + $var: checks (BNAConfig.validate depends on these) ────

def test_default_config_producer_consumer_and_vars_clean() -> None:
    cfg = BNAConfig.with_defaults()
    cfg.validate()  # exercises both gates end-to-end
    assert unproduced_references(cfg) == []
    assert undeclared_variables(cfg) == {}
    assert null_variables(cfg) == {}


def test_stage_declared_variables_vocabulary() -> None:
    # Foundation's fixed $var: vocabulary, discovered by walking PIPELINE modules.
    assert stage_declared_variables() == {
        "city_default_speed",
        "state_default_speed",
        "speed_bare_unit",
    }


def test_produced_fields_covers_base_and_providers() -> None:
    cfg = BNAConfig.with_defaults()
    produced = produced_fields(cfg)
    # base OSM tag, a stage-injected column, and a provider-derived field.
    assert "highway" in produced
    assert "adj_fc" in produced


def test_undeclared_variable_detected() -> None:
    cfg = BNAConfig.with_defaults()
    # Inject a rule referencing an undeclared $var: into the active segment ruleset.
    dec = load_decision({"name": "stress_segment", "passes": [{
        "name": "seg_stress",
        "rules": [{"id": "bogus",
                   "when": {"adj_fc": "residential", "speed_limit": {"gt": "$var:not_declared"}},
                   "set": {"ft_seg_stress": 3}}],
        "default": {"ft_seg_stress": 1},
    }]})
    cfg.stress.segment_rules = dec
    assert "not_declared" in undeclared_variables(cfg)


def test_orphan_override_detected() -> None:
    cfg = BNAConfig.with_defaults()
    referenced = set(variable_references(cfg))
    assert orphan_overrides(cfg, ["definitely_unused_knob"]) == ["definitely_unused_knob"]
    assert "definitely_unused_knob" not in referenced
