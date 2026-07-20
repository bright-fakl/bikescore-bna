"""A6 — DB-free acquisition: pure-logic unit tests + the provider-seam parity gate.

Two layers, both CI-safe (no network):

1. **Unit** — the deterministic pieces of the US provider: Geofabrik URL construction,
   FIPS→abbreviation mapping, ``CityIdentity.is_us``, the content-addressed
   ``store_file`` naming, and the PBF-cache sidecar round-trip.
2. **Provider-seam parity (the A6 gate)** — ``acquire_city`` is a thin wrapper over an
   :class:`InputProvider`. Feeding a stub provider that yields the frozen A0 oracle
   datasets through ``acquire_city`` and into ``score_city`` must reproduce the A5
   result *identically*. This proves the acquire → score wiring and the seam without a
   flaky live download (upstream OSM/census/LODES drift means a real re-acquire cannot
   be byte-reproducible; the live path is exercised by ``test_acquire_integration.py``,
   which the maintainer runs manually per the Aspen validation standard).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from bikescore import CityIdentity, acquire_city, build_config, score_city
from bikescore.acquire import (
    InputProvider,
    _build_geofabrik_url,
    _find_pbf_by_url,
    _geofabrik_url_for,
    _pbf_rel_path_from_url,
    _state_abbr_from_fips,
)
from bikescore.data_pool import store_file, update_ledger
from bikescore.deviations import KNOWN_DEVIATIONS
from bikescore.validation import compare_dataframes

ASPEN = CityIdentity(
    name="Aspen", slug="aspen-colorado", region="Colorado",
    country="united states", fips_code="0803620",
)

# ── Unit: Geofabrik URL construction ─────────────────────────────────────────


def test_geofabrik_url_us_state() -> None:
    assert _geofabrik_url_for("united states", "Colorado", "https://x") == (
        "https://x/north-america/us/colorado-latest.osm.pbf"
    )


def test_geofabrik_url_dc_slug() -> None:
    assert _geofabrik_url_for("us", "District of Columbia", "https://x") == (
        "https://x/north-america/us/district-of-columbia-latest.osm.pbf"
    )


def test_geofabrik_url_country_prefix_and_override() -> None:
    assert _geofabrik_url_for("united kingdom", None, "https://x") == (
        "https://x/europe/great-britain-latest.osm.pbf"
    )
    assert _geofabrik_url_for("france", None, "https://x") == (
        "https://x/europe/france-latest.osm.pbf"
    )


def test_build_geofabrik_url_from_city() -> None:
    assert _build_geofabrik_url(ASPEN).endswith("/north-america/us/colorado-latest.osm.pbf")


def test_pbf_rel_path_from_url() -> None:
    url = "https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf"
    assert _pbf_rel_path_from_url(url) == "north-america/us/colorado-latest.osm.pbf"


def test_state_abbr_from_fips() -> None:
    assert _state_abbr_from_fips("08") == "co"
    assert _state_abbr_from_fips("8") == "co"  # zfill
    assert _state_abbr_from_fips("99") is None


def test_city_is_us() -> None:
    assert ASPEN.is_us
    assert not CityIdentity(name="Paris", slug="paris", region="", country="France").is_us


# ── Unit: content-addressed pool ─────────────────────────────────────────────


def test_store_file_content_addressed_and_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    src1 = tmp_path / "a.bin"
    src1.write_bytes(b"hello")
    name1 = store_file(data_dir, "osm", src1, ".pbf")
    assert name1.startswith("osm-") and name1.endswith(".pbf")
    assert (data_dir / name1).read_bytes() == b"hello"

    # Same bytes → same name; second source is discarded.
    src2 = tmp_path / "b.bin"
    src2.write_bytes(b"hello")
    name2 = store_file(data_dir, "osm", src2, ".pbf")
    assert name2 == name1
    assert not src2.exists()


def test_update_ledger_accumulates(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir()
    (d / "osm-abc.pbf").write_bytes(b"x")
    update_ledger(d, "osm-abc.pbf", {"type": "osm"})
    update_ledger(d, "boundary-def.geojson", {"type": "boundary"})
    ledger = json.loads((d / "meta.json").read_text())
    assert set(ledger) == {"osm-abc.pbf", "boundary-def.geojson"}
    assert ledger["osm-abc.pbf"]["present"] is True
    assert ledger["boundary-def.geojson"]["present"] is False  # file not written


def test_find_pbf_by_url_roundtrip(tmp_path: Path) -> None:
    cache = tmp_path / "north-america" / "us"
    cache.mkdir(parents=True)
    pbf = cache / "colorado-20260610-abc.osm.pbf"
    pbf.write_bytes(b"pbf")
    url = "https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf"
    (Path(str(pbf) + ".meta.json")).write_text(json.dumps({
        "url": url, "downloaded_at": "2026-06-10T00:00:00+00:00",
        "size_bytes": 3, "sha256": "deadbeef",
    }))
    hit = _find_pbf_by_url(cache, url)
    assert hit is not None
    found_path, meta = hit
    assert found_path == pbf
    assert meta.url == url and meta.sha256 == "deadbeef"
    assert _find_pbf_by_url(cache, "https://other") is None


# ── Provider seam ────────────────────────────────────────────────────────────


class _StubProvider:
    """An :class:`InputProvider` that returns a fixed input dict (no network)."""

    def __init__(self, files: dict[str, Path]) -> None:
        self._files = files
        self.calls: list[tuple[CityIdentity, Path, bool]] = []

    def acquire(
        self, city: CityIdentity, out_dir: Path, *, force: bool = False,
    ) -> dict[str, Path]:
        self.calls.append((city, out_dir, force))
        return dict(self._files)


def test_acquire_city_delegates_to_provider(tmp_path: Path) -> None:
    """``acquire_city`` forwards city/out_dir/force to the injected provider verbatim."""
    stub = _StubProvider({"osm": tmp_path / "x.pbf"})
    assert isinstance(stub, InputProvider)  # structural Protocol check
    out = acquire_city(ASPEN, tmp_path / "data", provider=stub, force=True)
    assert out == {"osm": tmp_path / "x.pbf"}
    assert stub.calls == [(ASPEN, tmp_path / "data", True)]


# ── A6 gate: acquired inputs drive score_city to the A5 oracle ────────────────

ORACLE = Path(__file__).resolve().parent / "oracle" / "aspen"
_DEFAULT_DATASETS = Path(
    "/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado/datasets"
)


def _aspen_inputs() -> dict[str, Path] | None:
    d = Path(os.environ.get("BIKESCORE_ASPEN_DATASETS", _DEFAULT_DATASETS))
    if not d.is_dir():
        return None
    wanted = {
        "osm": "osm-*.pbf", "boundary": "boundary-*.geojson", "census": "census-*.parquet",
        "lodes_main": "lodes_main-*.csv", "lodes_aux": "lodes_aux-*.csv",
    }
    inputs: dict[str, Path] = {}
    for name, pat in wanted.items():
        hits = sorted(d.glob(pat))
        if not hits:
            return None
        inputs[name] = hits[0]
    return inputs


@pytest.mark.skipif(
    _aspen_inputs() is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS",
)
def test_acquire_city_output_scores_to_oracle() -> None:
    """acquire_city's returned inputs drive score_city to the identical A5 outputs.

    Uses a stub provider yielding the frozen oracle datasets so the check is
    deterministic; the assertion is the same identity gate as A5 (38f).
    """
    inputs = _aspen_inputs()
    assert inputs is not None
    stub = _StubProvider(inputs)

    acquired = acquire_city(ASPEN, Path("/unused"), provider=stub)
    assert {"osm", "boundary", "census", "lodes_main", "lodes_aux"} <= set(acquired)

    result = score_city(acquired, build_config("default"))
    for stage, filename, key in [
        ("scores", "scores.parquet", "geoid20"),
        ("neighborhood", "neighborhood.parquet", "score_id"),
    ]:
        computed = pd.read_parquet(result.output(stage, filename))
        reference = pd.read_parquet(ORACLE / stage / filename)
        cols = [c for c in reference.columns if c != key and c in computed.columns]
        report = compare_dataframes(
            computed, reference, stage=stage, city="aspen-colorado",
            key_col=key, columns=cols, deviations=KNOWN_DEVIATIONS,
        )
        assert report.passed and report.rows_differing == 0, f"{stage}/{filename} != oracle"
