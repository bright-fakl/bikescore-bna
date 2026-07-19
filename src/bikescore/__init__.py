"""bikescore — single-city bicycle network analysis as a database-free library.

Public API (the deliverable surface; filled across Phase 38 sub-phases A2-A7):

    build_config(scenario=None, overrides=None) -> BNAConfig   # 38b
    list_bundled_scenarios() -> list[str]                      # 38b
    score_city(inputs, config, *, pinned=None, to_stage=None)  # 38c/38f
    acquire_city(city, *, pbf_cache_dir=..., force=False)      # 38g

This package must never import from the orchestration layer (``bikescore_app``);
the dependency direction is app -> core only. See phases/38* in the bna-core repo.
"""

__version__ = "0.1.0"
