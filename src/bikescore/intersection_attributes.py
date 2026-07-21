"""Intersection attribute extensibility — YAML-configurable node matchers.

The parse stage tags each road node with a set of boolean intersection
attributes (``signalized``, ``stop``, ``rrfb``, ``island`` by default) that feed
the intersection-stress model. Historically these four checks were hard-coded in
``parse.py``'s ``node()`` handler and could not be changed for non-US cities
without editing Python.

This module makes the set data-driven: an :class:`IntersectionAttribute` is a
``name`` plus a row-level :class:`~bikescore.decision.Matcher` (the same
``any``-of-rows predicate destinations use). The four standard BNA attributes are
seeded by ``BNAConfig.with_defaults()``; scenarios/cities may add, replace, or
deactivate individual attributes via keyed merge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bikescore.decision import Matcher
from bikescore.destinations import _row, matcher

#: Attribute names the intersection-stress model (``rules/providers.py``) hard-codes.
#: Until that model is generalized (Decision 4 Phase 2), the active intersection
#: attributes must include these by name — they may be customized but not removed or
#: renamed, or the stress stage would fail. Enforced by ``BNAConfig.validate()``.
STRESS_REQUIRED_ATTRIBUTES: frozenset[str] = frozenset(
    {"signalized", "stop", "rrfb", "island"}
)


# ── Terse match (de)serialization ─────────────────────────────────────────────

def _clause_to_terse(clause: Any) -> tuple[str, Any]:
    """Reverse one :class:`Clause` back to a terse ``{field: value}`` pair.

    Inverse of :func:`bikescore.destinations._clause_from_cond`:
    ``present`` ⇒ ``None``; ``absent`` ⇒ ``"!"``; ``in`` ⇒ list; ``eq`` ⇒ scalar.
    Any other op is kept verbose so nothing is silently lost.
    """
    if clause.op == "present":
        return clause.field, None
    if clause.op == "absent":
        return clause.field, "!"
    if clause.op == "in":
        return clause.field, list(clause.value)
    if clause.op == "eq":
        return clause.field, clause.value
    return clause.field, {"op": clause.op, "value": clause.value}


def _matcher_to_terse(m: Matcher) -> list[dict]:
    """Serialize a Matcher back to a list of terse condition dicts (rows OR'd)."""
    return [dict(_clause_to_terse(c) for c in row.clauses) for row in m.rows]


def _parse_match(val: Any) -> Matcher:
    """Build a Matcher from terse rows (``[{tag: value}, ...]``) or a ``{any:[...]}`` dict."""
    if val is None:
        return Matcher(())
    if isinstance(val, Matcher):
        return val
    if isinstance(val, dict) and "any" in val:
        return Matcher.from_dict(val)
    if isinstance(val, list):
        return Matcher(tuple(_row(r) for r in val))
    raise ValueError(f"Invalid intersection attribute match: {val!r}")


# ── IntersectionAttribute ─────────────────────────────────────────────────────

@dataclass
class IntersectionAttribute:
    """One named boolean intersection attribute computed from node tags.

    ``match`` is an ``any``-of-rows matcher: a node gets the attribute if its tags
    match any row. ``name`` becomes a boolean column in ``nodes_df``.
    """

    name: str
    match: Matcher = field(default_factory=Matcher)
    enabled: bool = True

    def referenced_tags(self) -> set[str]:
        """OSM node tags this attribute's matcher reads."""
        return self.match.referenced_fields()

    def to_dict(self) -> dict:
        """Stable serialization with the match in terse list form."""
        return {
            "name": self.name,
            "match": _matcher_to_terse(self.match),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IntersectionAttribute:
        return cls(
            name=d["name"],
            match=_parse_match(d.get("match")),
            enabled=d.get("enabled", True),
        )


def default_intersection_attributes() -> list[IntersectionAttribute]:
    """The four standard BNA intersection attributes.

    Reproduces the exact boolean columns the legacy hard-coded ``node()`` handler
    produced (signalized / stop / rrfb / island).
    """
    return [
        IntersectionAttribute(
            "signalized",
            matcher({"highway": "traffic_signals"}),
        ),
        IntersectionAttribute(
            "stop",
            matcher({"highway": "stop", "stop": "all"}),
        ),
        IntersectionAttribute(
            "rrfb",
            matcher({"highway": "crossing",
                     "flashing_lights": ["yes", "button", "always", "sensor"]}),
        ),
        IntersectionAttribute(
            "island",
            matcher({"highway": "crossing", "crossing": "island"},
                    {"highway": "crossing", "crossing:island": "yes"}),
        ),
    ]


