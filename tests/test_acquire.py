"""A6 — DB-free acquisition: pure-logic unit tests + the provider-seam wiring.

Two layers, both CI-safe (no network):

1. **Unit** — the deterministic pieces of the US provider: Geofabrik URL construction,
   FIPS→abbreviation mapping, ``CityIdentity.is_us``, the content-addressed
   ``store_file`` naming, and the PBF-cache sidecar round-trip.
2. **Provider seam** — ``acquire_city`` is a thin wrapper over an
   :class:`InputProvider`; the seam test proves it forwards city/out_dir/force to the
   injected provider verbatim. The live download path is exercised by
   ``test_acquire_integration.py``, which the maintainer runs manually.
"""

from __future__ import annotations

import json
from pathlib import Path

from bikescore_bna import CityIdentity, acquire_city
from bikescore_bna.acquire import (
    InputProvider,
    _build_geofabrik_url,
    _find_pbf_by_url,
    _geofabrik_url_for,
    _pbf_rel_path_from_url,
    _state_abbr_from_fips,
)
from bikescore_bna.data_pool import store_file

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
        self,
        city: CityIdentity,
        out_dir: Path,
        *,
        force: bool = False,
        config: object | None = None,
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


def test_discover_inputs_maps_roles(tmp_path: Path) -> None:
    from bikescore_bna import discover_inputs

    d = tmp_path / "datasets"
    d.mkdir()
    (d / "osm-abc123.pbf").write_bytes(b"x")
    (d / "boundary-def456.geojson").write_text("{}")
    (d / "census-789abc.parquet").write_bytes(b"x")
    # non-input files ignored
    (d / "scores.parquet").write_bytes(b"x")

    inputs = discover_inputs(d)
    # analysis_boundary falls back to the boundary file when none was written separately.
    assert set(inputs) == {"osm", "boundary", "analysis_boundary", "census"}
    assert inputs["analysis_boundary"] == inputs["boundary"]
    assert inputs["osm"].name == "osm-abc123.pbf"
    assert discover_inputs(tmp_path / "nonexistent") == {}
