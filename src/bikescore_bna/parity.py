"""Stage-by-stage parity of a ``score_city`` result against a reference directory.

A *reference directory* holds one parquet per stage output at
``<reference_dir>/<stage>/<file>.parquet`` — the layout of ``tests/oracle/aspen`` and of
brokenspoke-analyzer exports. :func:`validate_result` runs each stage's computed output
through :func:`bikescore_bna.validation.compare_dataframes` with the right row-alignment
strategy and returns a report per stage.

This is a **development / QA** harness (parity against a ground-truth reference), reused
by the parity test suite and the ``bikescore-bna validate`` CLI. It is not part of the
scoring path — ``score_city`` never touches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from bikescore_bna.validation import ValidationReport, compare_dataframes

if TYPE_CHECKING:
    from bikescore_bna.deviations import KnownDeviation
    from bikescore_bna.pipeline import ScoreResult

# Serialized-geometry / list columns carry no scalar parity signal. ``geom_pt`` /
# ``geom_poly`` are EWKB-hex strings (they would slip past the dtype sniff), so they are
# excluded by name.
GEOM_COLS = frozenset({"geometry", "geometry_wkt", "geom_pt", "geom_poly"})

# case -> (rel_path, stage, key). ``key`` selects the row-alignment strategy:
#   str          → single natural key column
#   tuple[str,…] → composite key (joined into a synthetic ``_key``)
#   None         → sorted-multiset (no key; sort both by all comparable cols, align by
#                  position) — for edge tables that permit duplicate keys.
STAGE_FILES: dict[str, tuple[str, str, object]] = {
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


@dataclass
class StageParity:
    """One stage's parity outcome. ``report`` is ``None`` when the case was skipped."""

    case: str
    stage: str
    rel: str
    report: ValidationReport | None = None
    skip_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.report is not None and self.report.passed


def _discover_dest_cases(reference_dir: Path) -> dict[str, tuple[str, str, object]]:
    """Per-destination-type cluster tables (``dest_<type>.parquet``) found in the reference."""
    out: dict[str, tuple[str, str, object]] = {}
    for p in sorted((reference_dir / "destinations").glob("dest_*.parquet")):
        out[f"dest_{p.stem[len('dest_') :]}"] = (f"destinations/{p.name}", "destinations", "id")
    return out


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
    computed: pd.DataFrame, reference: pd.DataFrame, key: object
) -> tuple[pd.DataFrame, pd.DataFrame, str, list[str]]:
    """Return ``(computed, reference, key_col, columns)`` ready for ``compare_dataframes``."""
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


def compare_stage(
    result: ScoreResult,
    reference_dir: Path | str,
    case: str,
    rel: str,
    stage: str,
    key: object,
    *,
    city: str,
    deviations: list[KnownDeviation] | None = None,
) -> StageParity:
    """Compare one stage-output file against its reference; skip if either side is absent."""
    reference_dir = Path(reference_dir)
    ref_path = reference_dir / rel
    if not ref_path.exists():
        return StageParity(case, stage, rel, skip_reason="reference file absent")
    try:
        comp_path = result.output(stage, Path(rel).name)
    except KeyError:
        return StageParity(case, stage, rel, skip_reason="stage did not run")
    if not comp_path.exists():
        return StageParity(case, stage, rel, skip_reason="computed output absent")

    computed = pd.read_parquet(comp_path)
    reference = pd.read_parquet(ref_path)
    c, r, key_col, columns = _align(computed, reference, key)
    report = compare_dataframes(
        c, r, stage=stage, city=city, key_col=key_col, columns=columns, deviations=deviations
    )
    return StageParity(case, stage, rel, report=report)


def validate_result(
    result: ScoreResult,
    reference_dir: Path | str,
    *,
    city: str,
    stages: list[str] | None = None,
    deviations: list[KnownDeviation] | None = None,
) -> list[StageParity]:
    """Validate a ``score_city`` result against *reference_dir*, one report per stage output.

    Args:
        result: The ``ScoreResult`` from ``score_city`` (full run, or ``to_stage=...``).
        reference_dir: Directory with ``<stage>/<file>.parquet`` reference outputs.
        city: City label for the reports.
        stages: Optional filter — keep only cases whose stage name (or case name) is listed.
        deviations: Known SQL deviations to annotate as expected (e.g. ``KNOWN_DEVIATIONS``).

    Returns:
        One :class:`StageParity` per case, in pipeline order. Cases whose reference or
        computed file is absent are returned skipped (``report is None``).
    """
    reference_dir = Path(reference_dir)
    cases = {**STAGE_FILES, **_discover_dest_cases(reference_dir)}
    if stages is not None:
        want = set(stages)
        cases = {c: v for c, v in cases.items() if v[1] in want or c in want}
    return [
        compare_stage(result, reference_dir, case, rel, stage, key, city=city, deviations=deviations)
        for case, (rel, stage, key) in cases.items()
    ]
