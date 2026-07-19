"""Parse-stage constants needed by the foundation (Phase 38b stub).

Only ``BASE_WAY_TAGS`` is defined here for now — ``attributes`` imports it lazily while
building the default attribute registry. Phase 38d replaces this file with the full
parse stage (OSM → ways/nodes/POI network plane), which re-declares the same constant.
"""

from __future__ import annotations

BASE_WAY_TAGS: tuple[str, ...] = (
    # Parse's irreducible core: ``highway``/``bicycle`` drive way filtering
    # (``is_road``, the ``bicycle=no AND highway=path`` row drop) *before* any
    # attribute runs; ``name`` is the reserved output-label column. Everything else
    # a rule/provider consumes — including access/oneway/oneway:bicycle — is loaded
    # via a primary attribute in the scenario (Phase 35g).
    "highway", "bicycle", "name",
)
