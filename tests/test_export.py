"""Export framework: registration integrity, writers, and end-to-end on Aspen.

The unit tests need no data. The integration tests run the full ``score_city`` pipeline
once (module-scoped fixture) and export from that :class:`ScoreResult`; they ``skip`` when
the frozen Aspen input datasets are absent (cold clone / CI), mirroring
``test_score_city_e2e.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from bikescore_bna import (
    BNAConfig,
    ScoreResult,
    build_config,
    export_bundle,
    export_target,
    list_export_bundles,
    list_export_targets,
    score_city,
)
from bikescore_bna.city import CityIdentity
from bikescore_bna.export import (
    _EXPORT_BUNDLES,
    _EXPORT_TARGETS,
    ExportBundle,
    ExportContext,
    StageOutputNotFoundError,
    _register_bundle,
    validate_target_formats,
)

# ── Unit: registration integrity (no data) ───────────────────────────────────


def test_bundle_bna_registered() -> None:
    assert "bna" in list_export_bundles()
    assert set(_EXPORT_BUNDLES["bna"].targets) <= set(_EXPORT_TARGETS)


def test_stress_target_is_geo_and_in_bna() -> None:
    stress = _EXPORT_TARGETS["stress"]
    assert stress.kind == "geo"
    assert stress.owner_stage == "stress"
    assert "geojson" in (stress.formats or ["geojson", "shapefile", "csv"])
    assert "stress" in _EXPORT_BUNDLES["bna"].targets


def test_every_target_has_a_build() -> None:
    for name in list_export_targets():
        assert callable(_EXPORT_TARGETS[name].build), name


def test_validate_target_formats_rejects_unsupported() -> None:
    with pytest.raises(ValueError, match="does not support format"):
        validate_target_formats(_EXPORT_TARGETS["connectivity"], ["geojson"])  # plain target
    # geo target rejects a bogus format
    with pytest.raises(ValueError, match="does not support format"):
        validate_target_formats(_EXPORT_TARGETS["stress"], ["kml"])


def test_bundle_validation_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="unknown target"):
        _register_bundle(ExportBundle(name="_bad", targets={"nope": "nope"}))


def test_bundle_validation_rejects_required_not_subset() -> None:
    with pytest.raises(ValueError, match="not a subset"):
        _register_bundle(ExportBundle(
            name="_bad2", targets={"stress": "stress"}, required={"boundary"},
        ))


# ── Unit: writers via a synthetic context ─────────────────────────────────────


def _fake_context(tmp_path: Path) -> ExportContext:
    """A context whose ``stress`` stage dir holds a tiny 2-segment GeoDataFrame."""
    stress_dir = tmp_path / "stress"
    stress_dir.mkdir()
    gdf = gpd.GeoDataFrame(
        {
            "road_id": [1, 2],
            "ft_seg_stress": [1, 3],
            "start_node_id": [10, 11],
            "end_node_id": [11, 12],
        },
        geometry=[LineString([(0, 0), (1, 1)]), LineString([(1, 1), (2, 2)])],
        crs="EPSG:4326",
    )
    gdf.to_parquet(stress_dir / "stress.parquet")
    city = CityIdentity(
        name="Testville", slug="testville", region="Teststate", country="usa", fips_code="0812345",
    )
    return ExportContext(
        stage_dirs={"stress": stress_dir},
        dataset_paths={},
        city=city,
        config=build_config("default"),
    )


def test_write_stress_geojson_roundtrips(tmp_path: Path) -> None:
    ctx = _fake_context(tmp_path)
    from bikescore_bna.export import _write_target

    out = tmp_path / "out"
    out.mkdir()
    written = _write_target(_EXPORT_TARGETS["stress"], ctx, out, ["geojson"])
    assert len(written) == 1
    path = written[0]
    assert path.suffix == ".geojson"
    read_back = gpd.read_file(path)
    assert len(read_back) == 2
    assert read_back.crs.to_epsg() == 4326
    assert "ft_seg_stress" in read_back.columns


def test_write_stress_csv_drops_geometry(tmp_path: Path) -> None:
    ctx = _fake_context(tmp_path)
    from bikescore_bna.export import _write_target

    out = tmp_path / "out"
    out.mkdir()
    (path,) = _write_target(_EXPORT_TARGETS["stress"], ctx, out, ["csv"])
    df = pd.read_csv(path)
    assert "geometry" not in df.columns
    assert list(df["ft_seg_stress"]) == [1, 3]


def test_speed_limits_target_from_config(tmp_path: Path) -> None:
    ctx = _fake_context(tmp_path)
    df = _EXPORT_TARGETS["speed_limits"].build(ctx)
    assert list(df["city_fips_code"]) == ["0812345"]
    assert list(df["state_fips_code"]) == ["08"]


def test_missing_stage_raises(tmp_path: Path) -> None:
    ctx = ExportContext(
        stage_dirs={}, dataset_paths={},
        city=CityIdentity(name="X", slug="x", region="Y", country="usa"),
        config=build_config("default"),
    )
    with pytest.raises(StageOutputNotFoundError, match="was not part of this run"):
        ctx.stage_df("stress", "stress.parquet", geo=True)


# ── Integration: real Aspen run ───────────────────────────────────────────────

_DEFAULT_DATASETS = Path(
    "/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado/datasets"
)


def _aspen_inputs() -> dict[str, Path] | None:
    d = Path(os.environ.get("BIKESCORE_ASPEN_DATASETS", _DEFAULT_DATASETS))
    if not d.is_dir():
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
        hits = sorted(d.glob(pat))
        if not hits:
            return None
        inputs[name] = hits[0]
    return inputs


_ASPEN = _aspen_inputs()
integration = pytest.mark.skipif(
    _ASPEN is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS",
)


AspenRun = tuple[ScoreResult, CityIdentity, BNAConfig, dict[str, Path]]


@pytest.fixture(scope="module")
def aspen_run() -> AspenRun:
    inputs = _aspen_inputs()
    assert inputs is not None
    config = build_config("default")
    result = score_city(inputs, config)
    identity = CityIdentity(
        name="Aspen", slug="aspen", region="Colorado", country="usa", fips_code="0803620",
    )
    return result, identity, config, inputs


@integration
def test_export_stress_geojson_end_to_end(aspen_run: AspenRun, tmp_path: Path) -> None:
    result, identity, config, inputs = aspen_run
    written = export_target(
        result, identity, config, "stress", tmp_path, file_format="geojson", inputs=inputs,
    )
    assert len(written) == 1
    gdf = gpd.read_file(written[0])
    assert len(gdf) > 0
    assert gdf.crs.to_epsg() == 4326
    # The headline signal: per-segment LTS is present.
    assert any(c.endswith("seg_stress") or c == "ft_seg_lts" for c in gdf.columns), gdf.columns.tolist()


@integration
def test_export_bna_bundle_end_to_end(aspen_run: AspenRun, tmp_path: Path) -> None:
    result, identity, config, inputs = aspen_run
    written = export_bundle(result, identity, config, tmp_path, bundle="bna", inputs=inputs)
    names = {p.name for p in written}

    # Required geo deliverables landed, in the bundle's declared filenames/formats.
    assert "neighborhood_ways.geojson" in names
    assert "neighborhood_census_blocks.geojson" in names
    assert "neighborhood_connected_census_blocks.csv" in names
    assert "residential_speed_limit.csv" in names

    # README written and self-describing.
    readme = tmp_path / "README.md"
    assert readme.exists()
    assert "Aspen" in readme.read_text()

    # Every written geo file is valid + WGS84.
    for path in written:
        if path.suffix == ".geojson":
            gdf = gpd.read_file(path)
            assert gdf.crs.to_epsg() == 4326
