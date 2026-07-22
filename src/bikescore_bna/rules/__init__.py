"""Bundled BNA decisions + field catalogs (Phase 30d/30e).

The legacy ``RuleSet``/``Condition``/``Range`` engine has been superseded by the
canonical decision DSL in :mod:`bikescore_bna.decision`. This package now ships the
default decisions (classification, segment/intersection stress) as authored documents that compile to :class:`bikescore_bna.decision.Decision`,
plus the BNA derived-field providers, the typed field catalogs, and the
ruleset-name→catalog registry the analysis tools (Phase 30e) use.

Importing this package registers the providers as a side effect.
"""

from __future__ import annotations

from bikescore_bna.rules import providers as providers
from bikescore_bna.rules.catalogs import (
    IMPUTE_CATALOG,
    SEGMENT_CATALOG,
    catalog_for,
    impute_catalog,
    segment_catalog,
)
from bikescore_bna.rules.providers import ATTRIBUTES_CATALOG, INTERSECTION_CATALOG
from bikescore_bna.rules.stress_intersection import default_intersection_stress_rules
from bikescore_bna.rules.stress_segment import default_segment_stress_rules

__all__ = [
    "ATTRIBUTES_CATALOG",
    "IMPUTE_CATALOG",
    "INTERSECTION_CATALOG",
    "SEGMENT_CATALOG",
    "catalog_for",
    "default_intersection_stress_rules",
    "default_segment_stress_rules",
    "impute_catalog",
    "segment_catalog",
]
