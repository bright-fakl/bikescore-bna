"""Retired in Phase 35n.

``functional_class`` (and its derived flags ``access_ok`` / ``footway_wide`` /
``is_golf_path`` / ``cls_*``) are now ordinary computed attributes declared in
``data/attributes/standard-bna.yaml`` — the single source of truth. The former
``default_functional_class_rules`` loader and ``functional_class.yaml`` ruleset
were removed; nothing references them at runtime.
"""

from __future__ import annotations
