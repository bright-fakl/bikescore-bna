"""A5 — end-to-end ``score_city`` parity (the crux gate for the Phase 38 carve-out).

This is the load-bearing integration test. It proves the whole library split at once:
one ``score_city(inputs, build_config("default"))`` call drives the **complete** 11-stage
``PIPELINE`` — no ``to_stage`` cap, no DB, no ``graphlib``, no hashing, no run store — and
the final ``scores`` + ``neighborhood`` outputs must be **identical** to the frozen
bna-core Aspen oracle (run id ``01KWB7PKETZFS61JE1SNCHNMYW`` /
``gentle-gliding-gannet``, captured in ``tests/oracle/aspen/MANIFEST.json``).

"Identical" is stricter than the per-stage A4 gate in ``test_stages_parity.py``. The A0
oracle **is** bna-core's own output, so the only sanctioned differences are the
``KNOWN_DEVIATIONS`` that already applied *inside* bna-core when it produced the oracle —
and those all live in the ``attributes`` stage. By the time the pipeline reaches
``scores`` / ``neighborhood`` the oracle already bakes their downstream effect in, so the
faithful comparison here must invoke **zero** deviations: every final-output row must
match value-for-value. Any diff is a port bug (or a genuine algorithm difference to
escalate via ``/spec-issue``), not something to paper over — Part B does not start until
this is green.

The Aspen **input datasets** live in the frozen bna-core workspace, not this repo, so the
module ``skip``s when they are absent (cold clone / CI). Point at them with
``BIKESCORE_ASPEN_DATASETS`` or rely on the default workspace path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from bikescore_bna import BNAConfig, ScoreResult, build_config, score_city
from bikescore_bna.city import CityIdentity
from bikescore_bna.deviations import KNOWN_DEVIATIONS
from bikescore_bna.pipeline import PIPELINE
from bikescore_bna.state_speeds import resolve_city_speed_defaults
from bikescore_bna.validation import compare_dataframes

ORACLE = Path(__file__).resolve().parent / "oracle" / "aspen"
# Aspen locale identity — resolves the FIPS residential speed defaults the CLI/app
# apply, so the direct-``score_city`` parity run matches the (FIPS-corrected) oracle.
_ASPEN = CityIdentity(name="Aspen", slug="aspen-colorado", region="Colorado",
                     country="united states", fips_code="0803620")
_DEFAULT_DATASETS = Path(
    "/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado/datasets"
)

# Serialized-geometry / list columns are not scalar parity signal. The final scores +
# neighborhood tables carry none, but we filter defensively so a future column addition
# does not silently compare an EWKB-hex string.
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


pytestmark = pytest.mark.skipif(
    _aspen_inputs() is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS (see module docstring)",
)


@pytest.fixture(scope="module")
def full_run() -> ScoreResult:
    """One end-to-end run of the *complete* PIPELINE (no ``to_stage`` cap)."""
    inputs = _aspen_inputs()
    assert inputs is not None  # guarded by pytestmark
    config: BNAConfig = build_config("default")
    resolve_city_speed_defaults(config, _ASPEN)
    return score_city(inputs, config)


def test_full_pipeline_runs_every_stage(full_run: ScoreResult) -> None:
    """The DB-free driver ran all 11 stages clean, end-to-end, in one pass.

    This is the "complete carve-out" half of the gate: no partial run, every stage in
    ``PIPELINE`` produced an output directory that exists on disk.
    """
    expected = {stage.name for stage in PIPELINE}
    assert set(full_run.stage_dirs) == expected
    for name, path in full_run.stage_dirs.items():
        assert path.is_dir(), f"stage {name!r} produced no output dir at {path}"


# The headline final outputs, each keyed by its single natural key. These are the
# deliverables a downstream app reads; identity here is what proves the library is a
# faithful carve-out of the algorithm.
_FINAL_OUTPUTS: dict[str, tuple[str, str, str]] = {
    "scores": ("scores", "scores.parquet", "geoid20"),
    "neighborhood_overall": ("neighborhood", "neighborhood.parquet", "score_id"),
    "neighborhood_inputs": ("neighborhood", "score_inputs.parquet", "id"),
    "neighborhood_mileage": ("neighborhood", "mileage.parquet", "feature_type"),
}


def _scalar_columns(reference: pd.DataFrame, computed: pd.DataFrame, key: str) -> list[str]:
    """Shared columns minus the key and any geometry/list-valued columns."""
    cols: list[str] = []
    for c in reference.columns:
        if c == key or c in GEOM_COLS or c not in computed.columns:
            continue
        sample = reference[c].dropna()
        v = sample.iloc[0] if not sample.empty else None
        if isinstance(v, (list, tuple)) or type(v).__name__ == "ndarray" or hasattr(v, "geom_type"):
            continue
        cols.append(c)
    return cols


@pytest.mark.parametrize("case", list(_FINAL_OUTPUTS), ids=list(_FINAL_OUTPUTS))
def test_final_output_identical_to_oracle(case: str, full_run: ScoreResult) -> None:
    """Each final output equals the A0 oracle value-for-value, with zero deviations.

    Passing ``KNOWN_DEVIATIONS`` keeps the comparison faithful, but the assertion is
    stricter than :func:`ValidationReport.passed`: at these downstream stages the oracle
    already reflects every sanctioned deviation, so a correct port must produce **no**
    deviation-explained rows and **no** differing rows at all.
    """
    stage, filename, key = _FINAL_OUTPUTS[case]

    computed = pd.read_parquet(full_run.output(stage, filename))
    reference = pd.read_parquet(ORACLE / stage / filename)

    assert len(computed) == len(reference), (
        f"{stage}/{filename}: row count {len(computed)} != oracle {len(reference)}"
    )

    columns = _scalar_columns(reference, computed, key)
    report = compare_dataframes(
        computed,
        reference,
        stage=stage,
        city="aspen-colorado",
        key_col=key,
        columns=columns,
        deviations=KNOWN_DEVIATIONS,
    )
    if not report.passed or report.deviation_explained_rows or report.rows_differing:
        report.print()

    assert report.passed, (
        f"{stage}/{filename}: {report.rows_differing} differing rows, "
        f"{report.rows_only_computed} only-computed, {report.rows_only_reference} only-reference"
    )
    # Identity, not merely within-deviation: nothing downstream of `attributes` may need
    # a deviation to match, and no row may differ.
    assert report.deviation_explained_rows == 0, (
        f"{stage}/{filename}: {report.deviation_explained_rows} rows needed a KNOWN_DEVIATION "
        "to match — the final outputs must be identical to the oracle, not within-deviation"
    )
    assert report.rows_differing == 0, f"{stage}/{filename}: {report.rows_differing} differing rows"
    assert report.rows_only_computed == 0
    assert report.rows_only_reference == 0
