"""Default intersection-stress decision (Phase 30d — migrated to the decision DSL).

Two passes. ``int_stress`` (swept over direction) fires high stress for lesser /
tertiary subject roads that cross a high-stress road at an unsignalized, unstopped
node; its derived ``node_*`` / ``has_high_cross_*`` fields are computed on demand by
the ``intersection_context`` provider (the relocated cross-row join). The
``int_stress_link_reset`` pass (``after`` it) resets every ``_link`` road to 1,
matching ``stress_link_ints.sql``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bikescore_bna.decision import Decision, load_decision

_DATA_DIR = Path(__file__).parent / "data"


def _authored() -> dict[str, Any]:
    return {
        "name": "intersection_stress",
        "sets": {
            "lesser_fcs": ["residential", "unclassified", "living_street", "track", "path"],
            "link_fcs": ["motorway_link", "trunk_link", "primary_link",
                         "secondary_link", "tertiary_link"],
        },
        "predicates": {
            "unprotected_ft": {"node_signalized_ft": False, "node_stop_ft": False},
            "unprotected_tf": {"node_signalized_tf": False, "node_stop_tf": False},
        },
        "passes": [
            {
                "name": "int_stress",
                "for": {"dir": ["ft", "tf"]},
                "rules": [
                    {"id": "lesser_high_crossing",
                     "when": {"adj_fc": "$lesser_fcs", "use": "unprotected_<dir>",
                              "has_high_cross_<dir>": True},
                     "set": {"<dir>_int_stress": 3}},
                    {"id": "tertiary_high_crossing",
                     "when": {"adj_fc": "tertiary", "use": "unprotected_<dir>",
                              "has_high_cross_no_tert_<dir>": True},
                     "set": {"<dir>_int_stress": 3}},
                ],
                "default": {"<dir>_int_stress": 1},
            },
            {
                "name": "int_stress_link_reset",
                "after": "int_stress",
                "rules": [
                    {"id": "link_reset", "when": {"adj_fc": "$link_fcs"},
                     "set": {"ft_int_stress": 1, "tf_int_stress": 1}},
                ],
            },
        ],
    }


def authored_intersection_stress_doc() -> dict[str, Any]:
    """The authored *terse source* doc (Phase 36a): parsed YAML if present, else ``_authored()``.

    The compact authoring form — swept ``int_stress`` pass, ``$lesser_fcs``/``$link_fcs``
    set refs, ``after:`` link-reset pass — the rule-builder seeds/clones for this built-in.
    """
    path = _DATA_DIR / "intersection_stress.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return _authored()


def default_intersection_stress_rules() -> Decision:
    return load_decision(authored_intersection_stress_doc())
