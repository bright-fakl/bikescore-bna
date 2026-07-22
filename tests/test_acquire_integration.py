"""A6 — live acquisition integration test (manual / not run in CI).

Exercises the *real* :class:`~bikescore_bna.acquire.UsCensusLodesProvider`: downloads (or
reuses the cached) Colorado PBF, clips it to Aspen's boundary, and fetches census +
LODES, then drives the full pipeline. Marked ``integration`` + ``slow`` so it is
deselected by the default ``-m "not slow and not integration"`` addopts — run it
explicitly with ``uv run pytest -m integration``.

Reproducibility caveat (why this is *not* a byte-exact A5 gate): the Geofabrik OSM
extract, the pygris boundary/census, and LODES all evolve upstream, so a re-acquire
today need not reproduce the frozen A0 oracle byte-for-byte. Per the project's manual
Aspen validation standard, this test asserts the acquire → score path *runs clean and
produces a well-formed scores table*; the deterministic identity gate lives in
``test_acquire.py::test_acquire_city_output_scores_to_oracle`` (stub provider) and in
``test_score_city_e2e.py`` (frozen oracle inputs).

Point the shared PBF cache at a directory holding the regional extract to avoid the
~500 MB download: ``BIKESCORE_PBF_CACHE=/path/to/pbf`` (default: the global settings
cache dir).
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from bikescore_bna import CityIdentity, acquire_city, build_config, score_city

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ASPEN = CityIdentity(
    name="Aspen", slug="aspen-colorado", region="Colorado",
    country="united states", fips_code="0803620",
)


def _pbf_cache_dir() -> Path | None:
    env = os.environ.get("BIKESCORE_PBF_CACHE")
    return Path(env) if env else None


def test_live_acquire_then_score(tmp_path: Path) -> None:
    inputs = acquire_city(
        ASPEN, tmp_path / "data", pbf_cache_dir=_pbf_cache_dir(), force=False,
    )
    # US city → all five inputs present and non-empty.
    for name in ("osm", "boundary", "census", "lodes_main", "lodes_aux"):
        assert name in inputs, f"missing acquired input {name!r}"
        assert inputs[name].is_file() and inputs[name].stat().st_size > 0

    result = score_city(inputs, build_config("default"))
    scores = pd.read_parquet(result.output("scores", "scores.parquet"))
    assert len(scores) > 0
    assert "overall_score" in scores.columns
    assert scores["overall_score"].notna().any()
