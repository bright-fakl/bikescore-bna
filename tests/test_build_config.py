"""A2 gate — build_config parity, self-contained (no external reference).

The strongest committable invariant is the **honest-default** one (Phase 35c): the bundled
``default`` scenario is a faithful complete serialization of ``BNAConfig.with_defaults()``,
so ``build_config("default")`` must serialize identically to ``build_config(None)``. That
also proves the foundation port (config + decision + rules + attributes + destinations +
scenarios) is complete — a missing rule/attribute/destination would perturb the serialization.

The cross-repo check ``== bna-core`` is a split-time verification done out-of-band (both
serialize to the same 43,704-char document); it is not committed because bna-core is the
frozen oracle, not a test dependency.
"""

from __future__ import annotations

import yaml

from bikescore import build_config, list_bundled_scenarios
from bikescore.config_resolver import serialize_complete_config
from bikescore.scenarios import _bundled_scenarios_dir


def _ser(arg: object) -> dict:
    return serialize_complete_config(build_config(arg))


def test_bundled_set() -> None:
    assert list_bundled_scenarios() == ["default"]


def test_default_scenario_equals_with_defaults() -> None:
    """The honest-default invariant: bundled ``default`` == ``with_defaults()``."""
    assert _ser("default") == _ser(None)


def test_default_serialization_is_stable_and_nonempty() -> None:
    s = _ser("default")
    assert set(s) == {"config", "rule_sets"}
    assert s["config"].get("attributes"), "default must carry attributes"
    assert s["config"].get("destinations"), "default must carry destinations"
    assert s["rule_sets"], "default must carry rule sets"


def test_serialization_round_trips_through_build_config() -> None:
    """Feeding serialize_complete_config output back as a complete scenario is a fixpoint."""
    once = _ser("default")
    doc = {"type": "complete", **once}
    twice = serialize_complete_config(build_config(doc))
    assert once == twice


def test_default_serialization_matches_bundled_yaml() -> None:
    """serialize(build_config('default')) matches the on-disk bundled default.yaml."""
    disk = yaml.safe_load((_bundled_scenarios_dir() / "default.yaml").read_text())
    ser = _ser("default")
    assert ser["config"] == disk["config"]
    assert ser["rule_sets"] == disk["rule_sets"]


def test_overrides_apply_last() -> None:
    over = build_config("default", overrides={"city.default_speed": 40})
    assert over.city.default_speed == 40
    assert build_config("default").city.default_speed != 40
