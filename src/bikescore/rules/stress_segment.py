"""Default segment-stress decision (Phase 30d — migrated to the decision DSL).

One pass with a ``for: {dir: [ft, tf]}`` sweep: every rule sets ``<dir>_seg_stress``
so both travel directions are produced from one authored block. The primary/secondary/
tertiary section is authored once as a nested scope-gated tree under a block
``for: {fcg}`` sweep (Phase 36c) -- it compiles to the same flat per-group leaves the
three hand-written blocks produced. Because functional classes are disjoint, within-group
rule order (track → buffered → lane → shared → default) is what matters and is
preserved. Mirrors the 8 segment-stress SQL files; ``stress_one_way_reset.sql`` is
applied as Python mechanics in the stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bikescore.decision import Decision, load_decision

_DATA_DIR = Path(__file__).parent / "data"


def _fc_group_block() -> dict[str, Any]:
    """The fc-group section: one nested scope-gated tree swept over the three fc-groups.

    The block ``for: {fcg}`` composes (cartesian) with the pass-level ``{dir}`` sweep.
    The scope gate ``adj_fc in $<fcg>_fcs`` shields each group; inside, the tree branches
    on ``<dir>_bike_infra`` with low/high threshold sub-branches and a scope default of 3.
    Compiles to the same flat per-group track/buffered/lane/shared/default leaves the
    three hand-written blocks produced (Phase 36c)."""
    return {
        "for": {"fcg": ["primary", "secondary", "tertiary"]},
        "when": {"adj_fc": "$<fcg>_fcs"},
        "rules": [
            {"id": "<fcg>_track",
             "when": {"<dir>_bike_infra": "track"},
             "set": {"<dir>_seg_stress": 1}},
            {"when": {"<dir>_bike_infra": "buffered_lane"},
             "rules": [
                 {"id": "<fcg>_buffered_low",
                  "when": {"speed_limit": {"le": 25}, "<dir>_lanes": {"le": 1}},
                  "set": {"<dir>_seg_stress": 1}},
                 {"id": "<fcg>_buffered_high",
                  "set": {"<dir>_seg_stress": 3}},
             ]},
            {"when": {"<dir>_bike_infra": "lane"},
             "rules": [
                 {"id": "<fcg>_lane_low",
                  "when": {"speed_limit": {"le": 20}, "<dir>_lanes": {"le": 1},
                           "<dir>_bike_infra_width": {"ge": 4}},
                  "set": {"<dir>_seg_stress": 1}},
                 {"id": "<fcg>_lane_high",
                  "set": {"<dir>_seg_stress": 3}},
             ]},
            {"id": "<fcg>_shared_low",
             "when": {"speed_limit": {"le": 15}, "<dir>_lanes": 1},
             "set": {"<dir>_seg_stress": 1}},
            {"id": "<fcg>_default",
             "set": {"<dir>_seg_stress": 3}},
        ],
    }


def _low_high_block(fc: str) -> dict[str, Any]:
    """A ``adj_fc == fc`` scope with a speed<=25 low branch and a scope default of 3."""
    return {
        "id": fc,
        "when": {"adj_fc": fc},
        "rules": [
            {"id": f"{fc}_low",
             "when": {"speed_limit": {"le": 25}},
             "set": {"<dir>_seg_stress": 1}},
            {"id": f"{fc}_high",
             "set": {"<dir>_seg_stress": 3}},
        ],
    }


def _authored() -> dict[str, Any]:
    rules: list[dict[str, Any]] = [
        {"id": "motorway_trunk", "when": {"adj_fc": "$motorway_trunk_fcs"},
         "set": {"<dir>_seg_stress": 3}},
        {"id": "path_track", "when": {"adj_fc": ["path", "track"]},
         "set": {"<dir>_seg_stress": 1}},
        {"id": "living_street_bicycle_no",
         "when": {"adj_fc": "living_street", "bicycle": "no"},
         "set": {"<dir>_seg_stress": 3}},
        {"id": "living_street", "when": {"adj_fc": "living_street"},
         "set": {"<dir>_seg_stress": 1}},
        _fc_group_block(),
        _low_high_block("residential"),
        _low_high_block("unclassified"),
    ]
    return {
        "name": "segment_stress",
        "sets": {
            "motorway_trunk_fcs": ["motorway", "trunk", "motorway_link", "trunk_link"],
            "primary_fcs": ["primary", "primary_link"],
            "secondary_fcs": ["secondary", "secondary_link"],
            "tertiary_fcs": ["tertiary", "tertiary_link"],
        },
        "passes": [{"name": "seg_stress", "for": {"dir": ["ft", "tf"]}, "rules": rules}],
    }


def authored_segment_stress_doc() -> dict[str, Any]:
    """The authored *terse source* doc (Phase 36a): parsed YAML if present, else ``_authored()``.

    The compact authoring form — ``for: {dir}`` sweep, ``$..._fcs`` set refs — that the
    rule-builder seeds and clones for this built-in ruleset, not a canonical re-render.
    """
    path = _DATA_DIR / "segment_stress.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return _authored()


def default_segment_stress_rules() -> Decision:
    return load_decision(authored_segment_stress_doc())
