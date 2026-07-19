"""A4 walking-skeleton parity gate — ``parse → census → jobs → attributes → segment →
stress`` output must match the frozen bna-core Aspen oracle.

One ``score_city(inputs, build_config("default"), to_stage="stress")`` run drives all six
stages into a temp dir (no DB, no run store); each stage's output is then compared against
its ``tests/oracle/aspen/<stage>/`` reference parquet with core's own ``compare_dataframes``
(within ``KNOWN_DEVIATIONS``). ``stress.parquet`` is the headline gate.

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


# Each stage: (parquet filename, key column). Comparison auto-excludes geometry and
# list-valued columns (node_ids); everything else — tags, flags, stress levels — is
# compared exactly (tolerance 0) against the oracle.
_STAGE_FILES: dict[str, tuple[str, str]] = {
    "parse_ways": ("parse/ways_raw.parquet", "osm_id"),
    "parse_nodes": ("parse/nodes.parquet", "node_id"),
    "census": ("census/census_blocks.parquet", "geoid20"),
    "jobs": ("jobs/jobs.parquet", "blockid20"),
    "attributes": ("attributes/ways_classified.parquet", "osm_id"),
    "segment": ("segment/segments.parquet", "road_id"),
    "stress": ("stress/stress.parquet", "road_id"),
}

# Which pipeline stage produced each file (for score_city output lookup).
_STAGE_OF_FILE = {
    "parse_ways": "parse",
    "parse_nodes": "parse",
    "census": "census",
    "jobs": "jobs",
    "attributes": "attributes",
    "segment": "segment",
    "stress": "stress",
}

pytestmark = pytest.mark.skipif(
    _aspen_inputs() is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS (see module docstring)",
)


@pytest.fixture(scope="module")
def score_result() -> ScoreResult:
    inputs = _aspen_inputs()
    assert inputs is not None  # guarded by pytestmark
    config: BNAConfig = build_config("default")
    return score_city(inputs, config, to_stage="stress")


def _comparable_columns(computed: pd.DataFrame, reference: pd.DataFrame, key_col: str) -> list[str]:
    """Shared columns minus geometry and list-valued columns (compared elsewhere / not scalar)."""
    shared = [c for c in reference.columns if c in computed.columns and c != key_col]
    cols: list[str] = []
    for c in shared:
        if c == "geometry":
            continue
        sample = reference[c].dropna()
        v = sample.iloc[0] if not sample.empty else None
        if isinstance(v, (list, tuple)) or type(v).__name__ == "ndarray" or hasattr(v, "geom_type"):
            continue
        cols.append(c)
    return cols


@pytest.mark.parametrize("case", list(_STAGE_FILES), ids=list(_STAGE_FILES))
def test_stage_matches_oracle(case: str, score_result: ScoreResult) -> None:
    rel, key_col = _STAGE_FILES[case]
    stage = _STAGE_OF_FILE[case]
    filename = Path(rel).name

    computed = pd.read_parquet(score_result.output(stage, filename))
    reference = pd.read_parquet(ORACLE / rel)

    assert len(computed) == len(reference), (
        f"{rel}: row count {len(computed)} != oracle {len(reference)}"
    )

    columns = _comparable_columns(computed, reference, key_col)
    report = compare_dataframes(
        computed, reference,
        stage=stage, city="aspen-colorado",
        key_col=key_col, columns=columns,
        deviations=KNOWN_DEVIATIONS,
    )
    if not report.passed:
        report.print()
    assert report.passed, (
        f"{rel}: {report.rows_differing} differing rows, "
        f"{report.rows_only_computed} only-computed, {report.rows_only_reference} only-reference"
    )
