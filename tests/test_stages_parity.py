"""A4 per-stage parity gate — the full 11-stage pipeline ``parse → … → neighborhood``
output must match the frozen bna-core Aspen oracle.

One ``score_city(inputs, build_config("default"), to_stage="neighborhood")`` run drives
every stage into a temp dir (no DB, no run store); each stage's output parquet is then
compared against its ``tests/oracle/aspen/<stage>/`` reference with core's own
``compare_dataframes`` (within ``KNOWN_DEVIATIONS``). ``stress.parquet``, ``scores.parquet``
and the three ``neighborhood`` tables are the headline gates. This phase (38e) gates each
stage *independently*; end-to-end ``score_city`` identity is proven in 38f.

Row alignment varies per file: most tables have a single natural key; the two edge/pair
tables (``graph/graph.parquet`` — parallel edges allowed; ``connectivity``) use a
composite or sorted-multiset key instead.

The Aspen **input datasets** live in the frozen bna-core workspace, not this repo, so the
whole module ``skip``s when they are absent (cold clone / CI). Point at them with
``BIKESCORE_ASPEN_DATASETS`` or rely on the default workspace path. Oracle *outputs* are
regenerated via ``tests/oracle/regenerate_aspen.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from bikescore import BNAConfig, ScoreResult, build_config, score_city
from bikescore.deviations import KNOWN_DEVIATIONS
from bikescore.validation import compare_dataframes

ORACLE = Path(__file__).resolve().parent / "oracle" / "aspen"
_DEFAULT_DATASETS = Path(
    "/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado/datasets"
)

# Serialized-geometry / list columns are not scalar parity signal — compared elsewhere
# or not at all. ``geom_pt`` / ``geom_poly`` are EWKB-hex strings (would slip past the
# dtype sniff in ``_comparable_columns``), so they are excluded by name.
GEOM_COLS = frozenset({"geometry", "geometry_wkt", "geom_pt", "geom_poly"})


def _datasets_dir() -> Path | None:
    d = Path(os.environ.get("BIKESCORE_ASPEN_DATASETS", _DEFAULT_DATASETS))
    return d if d.is_dir() else None


def _one(dir_: Path, pattern: str) -> Path | None:
    hits = sorted(dir_.glob(pattern))
    return hits[0] if hits else None


def _aspen_inputs() -> dict[str, Path] | None:
    d = _datasets_dir()
    if d is None:
        return None
    wanted = {
        "osm": "osm-*.pbf",
        "boundary": "boundary-*.geojson",
        "census": "census-*.parquet",
        "lodes_main": "lodes_main-*.csv",
        "lodes_aux": "lodes_aux-*.csv",
    }
    inputs: dict[str, Path] = {}
    for name, pat in wanted.items():
        p = _one(d, pat)
        if p is None:
            return None
        inputs[name] = p
    return inputs


# case -> (rel_path, stage, key). ``key`` selects the row-alignment strategy:
#   str          → single natural key column
#   tuple[str,…] → composite key (joined into a synthetic ``_key``)
#   None         → sorted-multiset (no key; sort both by all comparable cols, align by
#                  position) — for edge tables that permit duplicate keys.
_STAGE_FILES: dict[str, tuple[str, str, object]] = {
    "parse_ways": ("parse/ways_raw.parquet", "parse", "osm_id"),
    "parse_nodes": ("parse/nodes.parquet", "parse", "node_id"),
    "census": ("census/census_blocks.parquet", "census", "geoid20"),
    "jobs": ("jobs/jobs.parquet", "jobs", "blockid20"),
    "attributes": ("attributes/ways_classified.parquet", "attributes", "osm_id"),
    "segment": ("segment/segments.parquet", "segment", "road_id"),
    "stress": ("stress/stress.parquet", "stress", "road_id"),
    "graph_links": ("graph/graph.parquet", "graph", None),
    "graph_nodes": ("graph/nodes.parquet", "graph", "vert_id"),
    "graph_blocks": ("graph/blocks_with_roads.parquet", "graph", "geoid20"),
    "connectivity": (
        "connectivity/connectivity.parquet",
        "connectivity",
        ("source_blockid20", "target_blockid20"),
    ),
    "destinations_summary": (
        "destinations/destination_summary.parquet",
        "destinations",
        "dest_type",
    ),
    "scores": ("scores/scores.parquet", "scores", "geoid20"),
    "neighborhood_overall": ("neighborhood/neighborhood.parquet", "neighborhood", "score_id"),
    "neighborhood_inputs": ("neighborhood/score_inputs.parquet", "neighborhood", "id"),
    "neighborhood_mileage": ("neighborhood/mileage.parquet", "neighborhood", "feature_type"),
}

# Per-destination-type cluster tables (dest_<type>.parquet). Discovered from the oracle so
# every acquired destination type is gated on its cluster id-set + (placeholder) pop columns.
for _p in sorted((ORACLE / "destinations").glob("dest_*.parquet")):
    _STAGE_FILES[f"dest_{_p.stem[len('dest_') :]}"] = (
        f"destinations/{_p.name}",
        "destinations",
        "id",
    )


pytestmark = pytest.mark.skipif(
    _aspen_inputs() is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS (see module docstring)",
)


@pytest.fixture(scope="module")
def score_result() -> ScoreResult:
    inputs = _aspen_inputs()
    assert inputs is not None  # guarded by pytestmark
    config: BNAConfig = build_config("default")
    return score_city(inputs, config, to_stage="neighborhood")


def _comparable_columns(
    reference: pd.DataFrame, computed: pd.DataFrame, exclude: set[str]
) -> list[str]:
    """Shared columns minus geometry/serialized-geometry and list-valued columns."""
    cols: list[str] = []
    for c in reference.columns:
        if c in exclude or c not in computed.columns or c in GEOM_COLS:
            continue
        sample = reference[c].dropna()
        v = sample.iloc[0] if not sample.empty else None
        if isinstance(v, (list, tuple)) or type(v).__name__ == "ndarray" or hasattr(v, "geom_type"):
            continue
        cols.append(c)
    return cols


def _align(
    computed: pd.DataFrame,
    reference: pd.DataFrame,
    key: object,
) -> tuple[pd.DataFrame, pd.DataFrame, str, list[str]]:
    """Return (computed, reference, key_col, columns) ready for ``compare_dataframes``."""
    if isinstance(key, str):
        cols = _comparable_columns(reference, computed, exclude={key})
        return computed, reference, key, cols
    if isinstance(key, tuple):
        cols = _comparable_columns(reference, computed, exclude=set(key))
        c = computed.copy()
        r = reference.copy()
        c["_key"] = c[list(key)].astype(str).agg("|".join, axis=1)
        r["_key"] = r[list(key)].astype(str).agg("|".join, axis=1)
        return c, r, "_key", cols
    # key is None → sorted-multiset alignment by position
    cols = _comparable_columns(reference, computed, exclude=set())
    c = computed.sort_values(cols, na_position="last").reset_index(drop=True)
    r = reference.sort_values(cols, na_position="last").reset_index(drop=True)
    c["_rowkey"] = range(len(c))
    r["_rowkey"] = range(len(r))
    return c, r, "_rowkey", cols


@pytest.mark.parametrize("case", list(_STAGE_FILES), ids=list(_STAGE_FILES))
def test_stage_matches_oracle(case: str, score_result: ScoreResult) -> None:
    rel, stage, key = _STAGE_FILES[case]
    filename = Path(rel).name

    computed = pd.read_parquet(score_result.output(stage, filename))
    reference = pd.read_parquet(ORACLE / rel)

    assert len(computed) == len(reference), (
        f"{rel}: row count {len(computed)} != oracle {len(reference)}"
    )

    computed, reference, key_col, columns = _align(computed, reference, key)
    report = compare_dataframes(
        computed,
        reference,
        stage=stage,
        city="aspen-colorado",
        key_col=key_col,
        columns=columns,
        deviations=KNOWN_DEVIATIONS,
    )
    if not report.passed:
        report.print()
    assert report.passed, (
        f"{rel}: {report.rows_differing} differing rows, "
        f"{report.rows_only_computed} only-computed, {report.rows_only_reference} only-reference"
    )
