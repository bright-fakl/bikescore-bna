"""bikescore — single-city bicycle network analysis as a database-free library.

Public API (the deliverable surface; filled across Phase 38 sub-phases A2-A7):

    build_config(scenario=None, overrides=None) -> BNAConfig   # 38b
    list_bundled_scenarios() -> list[str]                      # 38b
    score_city(inputs, config, *, pinned=None, to_stage=None)  # 38c/38f
    acquire_city(city, out_dir, *, pbf_cache_dir=..., force=False)  # 38g

This package must never import from the orchestration layer (``bikescore_app``);
the dependency direction is app -> core only. See phases/38* in the bna-core repo.
"""

__version__ = "0.1.0"


from bikescore.acquire import InputProvider, acquire_city
from bikescore.city import CityIdentity, load_city
from bikescore.config import BNAConfig
from bikescore.config_resolver import build_config
from bikescore.export import (
    ExportContext,
    export_bundle,
    export_target,
    list_export_bundles,
    list_export_targets,
)
from bikescore.pipeline import PIPELINE, ScoreResult, score_city
from bikescore.scenarios import list_bundled_scenarios
from bikescore.stage import StageSpec, run_stage

__all__ = [
    "PIPELINE",
    "BNAConfig",
    "CityIdentity",
    "ExportContext",
    "InputProvider",
    "ScoreResult",
    "StageSpec",
    "__version__",
    "acquire_city",
    "build_config",
    "export_bundle",
    "export_target",
    "list_bundled_scenarios",
    "list_export_bundles",
    "list_export_targets",
    "load_city",
    "run_stage",
    "score_city",
]
