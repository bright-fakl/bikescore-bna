"""Per-ruleset typed field catalogs + a name→catalog registry (Phase 30e).

The classify and intersection catalogs already live in :mod:`bikescore.rules.providers`
(they own the derived-field providers). This module adds the remaining typed
catalogs (segment stress, imputation) and a single registry mapping a *ruleset
name* (the CLI/web vocabulary, e.g. ``stress_segment``) to its catalog, so the
analysis tools (contexts, validation) can type-check and partition any ruleset.
"""

from __future__ import annotations

from bikescore.decision import FieldCatalog
from bikescore.rules.providers import INTERSECTION_CATALOG


def segment_catalog() -> FieldCatalog:
    cat = FieldCatalog("segment")
    cat.frame("adj_fc", "enum").frame("bicycle", "str").frame("speed_limit", "int")
    for d in ("ft", "tf"):
        cat.frame(f"{d}_bike_infra", "enum")
        cat.frame(f"{d}_lanes", "int")
        cat.frame(f"{d}_bike_infra_width", "float")
    return cat


def impute_catalog() -> FieldCatalog:
    cat = FieldCatalog("impute")
    cat.frame("functional_class", "enum")
    cat.frame("speed_limit", "int")
    cat.frame("ft_lanes", "int").frame("tf_lanes", "int")
    cat.frame("width_ft", "float")
    return cat


SEGMENT_CATALOG = segment_catalog()
IMPUTE_CATALOG = impute_catalog()

#: ruleset name (CLI vocabulary) → typed catalog.
_CATALOG_BY_RULESET: dict[str, FieldCatalog] = {
    "stress_segment": SEGMENT_CATALOG,
    "stress_intersection": INTERSECTION_CATALOG,
}


def catalog_for(ruleset_name: str) -> FieldCatalog | None:
    """Return the typed catalog for a ruleset name, or ``None`` if unknown."""
    return _CATALOG_BY_RULESET.get(ruleset_name)
