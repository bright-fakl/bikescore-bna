"""Phase 1 boundary-manipulation tests: transforms, excluded-block layer, wiring.

Self-contained (crafted fixtures, no Aspen datasets) so they run in normal CI. The
central guarantee under test is oracle parity: with no boundary transform configured,
the analysis boundary is the fetched boundary and the scored ``census_blocks`` output
is byte-for-byte unchanged. The excluded-block layer and analysis-boundary export are
inert additions the scoring path never sees.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from bikescore_bna.boundary import has_boundary_transforms, prepare_boundary
from bikescore_bna.config import BNAConfig, BoundaryConfig, ConfigValidationError
from bikescore_bna.config_resolver import build_config
from bikescore_bna.export import (
    _EXPORT_TARGETS,
    ExportContext,
    StageOutputNotFoundError,
    list_export_targets,
)
from bikescore_bna.stages import census

_SRID = 32618  # UTM 18N — deterministic metric CRS for the fixtures below.


def _cfg(**boundary_kw: object) -> BNAConfig:
    c = BNAConfig.with_defaults()
    c.output_srid = _SRID
    c.boundary = BoundaryConfig(**boundary_kw)
    return c


def _multi_with_hole_and_island() -> gpd.GeoDataFrame:
    """A big square with an interior hole, plus a small detached island (MultiPolygon)."""
    outer = Polygon([(0, 0), (0, 0.1), (0.1, 0.1), (0.1, 0)])
    hole = Polygon([(0.02, 0.02), (0.02, 0.04), (0.04, 0.04), (0.04, 0.02)])
    island = Polygon([(0.2, 0.2), (0.2, 0.21), (0.21, 0.21), (0.21, 0.2)])
    return gpd.GeoDataFrame(geometry=[MultiPolygon([outer.difference(hole), island])], crs=4326)


# ── prepare_boundary transforms ───────────────────────────────────────────────


def test_has_boundary_transforms() -> None:
    assert has_boundary_transforms(BoundaryConfig()) is False
    assert has_boundary_transforms(BoundaryConfig(fill_holes=True)) is True
    assert has_boundary_transforms(BoundaryConfig(keep_largest_part=True)) is True
    assert has_boundary_transforms(BoundaryConfig(clip_shape="box", clip_size_m=100)) is True


def test_identity_returns_same_object_when_valid_and_no_transform() -> None:
    src = _multi_with_hole_and_island()
    result = prepare_boundary(src, _cfg())
    assert result is src  # identity fast-path → caller can reuse the source file


def test_invalid_geometry_is_repaired_even_without_transform() -> None:
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])  # self-intersecting
    src = gpd.GeoDataFrame(geometry=[bowtie], crs=4326)
    assert not bool(src.geometry.is_valid.all())

    result = prepare_boundary(src, _cfg())
    assert result is not src
    assert bool(result.geometry.is_valid.all())


def test_fill_holes_removes_interior_rings() -> None:
    result = prepare_boundary(_multi_with_hole_and_island(), _cfg(fill_holes=True))
    geom = result.geometry.iloc[0]
    parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    assert sum(len(p.interiors) for p in parts) == 0


def test_keep_largest_part_drops_island() -> None:
    result = prepare_boundary(_multi_with_hole_and_island(), _cfg(keep_largest_part=True))
    geom = result.geometry.iloc[0]
    assert geom.geom_type == "Polygon"  # island dropped
    assert len(geom.interiors) == 1  # hole preserved (fill_holes not set)


def test_clip_box_is_bounded_and_intersected() -> None:
    src = _multi_with_hole_and_island()
    result = prepare_boundary(src, _cfg(clip_shape="box", clip_size_m=5000.0))
    clipped = result.to_crs(_SRID).geometry.iloc[0]
    # Box side is exactly clip_size_m; intersected with boundary so area stays > 0
    # and never exceeds the original boundary area.
    assert round(clipped.bounds[2] - clipped.bounds[0]) <= 5000
    assert clipped.area > 0
    assert clipped.area <= src.to_crs(_SRID).geometry.iloc[0].area + 1.0


def test_clip_circle_is_bounded() -> None:
    src = _multi_with_hole_and_island()
    result = prepare_boundary(src, _cfg(clip_shape="circle", clip_size_m=5000.0))
    clipped = result.to_crs(_SRID).geometry.iloc[0]
    assert clipped.area > 0
    assert (clipped.bounds[2] - clipped.bounds[0]) <= 5000 + 1.0  # diameter cap


# ── config validation + resolver wiring ──────────────────────────────────────


def test_clip_shape_and_size_must_be_paired() -> None:
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(clip_shape="box").validate()
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(clip_size_m=100).validate()
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(clip_shape="box", clip_size_m=-5).validate()
    BoundaryConfig(clip_shape="box", clip_size_m=100).validate()  # ok


def test_boundary_namespace_resolves_from_config() -> None:
    c = build_config(
        "default",
        {"boundary.fill_holes": True, "boundary.clip_shape": "circle", "boundary.clip_size_m": 500},
    )
    assert c.boundary.fill_holes is True
    assert c.boundary.clip_shape == "circle"
    assert c.boundary.clip_size_m == 500


# ── census: block_class + excluded layer ──────────────────────────────────────


def _census_fixture() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    boundary = gpd.GeoDataFrame(geometry=[box(0, 0, 0.1, 0.1)], crs=4326)
    blocks = gpd.GeoDataFrame(
        {
            "geoid20": ["A", "B", "C", "D"],
            "pop20": [10, 20, 0, 5],
            "aland20": [100, 100, 0, 100],  # C is water
            "awater20": [0, 0, 50, 0],
            "geometry": [
                box(0.01, 0.01, 0.02, 0.02),    # A fully inside → included
                box(0.095, 0.095, 0.15, 0.15),  # B mostly outside → outside
                box(0.03, 0.03, 0.04, 0.04),    # C water → water
                box(0.05, 0.05, 0.06, 0.06),    # D inside → included
            ],
        },
        crs=4326,
    )
    return blocks, boundary


def test_classify_assigns_water_outside_included() -> None:
    blocks, boundary = _census_fixture()
    classified = census.classify_census_blocks(blocks, boundary, 0.50, True)
    by_id = dict(zip(classified["geoid20"], classified["block_class"]))
    assert by_id == {"A": "included", "B": "outside", "C": "water", "D": "included"}


def test_filter_is_byte_identical_to_classify_included() -> None:
    """Parity guard: filter_census_blocks == classify's ``included`` subset, unchanged."""
    blocks, boundary = _census_fixture()
    included_filter = census.filter_census_blocks(blocks, boundary, 0.50, True)
    classified = census.classify_census_blocks(blocks, boundary, 0.50, True)
    included_classify = (
        classified[classified["block_class"] == "included"]
        .drop(columns="block_class")
        .reset_index(drop=True)
    )
    assert "block_class" not in included_filter.columns
    assert included_filter.equals(included_classify)


def _write_census_inputs(tmp_path: Path) -> dict[str, Path]:
    blocks, boundary = _census_fixture()
    census_path = tmp_path / "census.parquet"
    boundary_path = tmp_path / "boundary.geojson"
    blocks.to_parquet(census_path)
    boundary.to_file(boundary_path, driver="GeoJSON")
    return {
        "dataset:census": census_path,
        "dataset:analysis_boundary": boundary_path,
    }


def test_census_run_writes_included_and_excluded(tmp_path: Path) -> None:
    inputs = _write_census_inputs(tmp_path)
    out = tmp_path / "census_out"
    census._run(inputs, out, BNAConfig.with_defaults())

    included = gpd.read_parquet(out / "census_blocks.parquet")
    assert set(included["geoid20"]) == {"A", "D"}
    assert "block_class" not in included.columns  # scored output unchanged

    excluded = gpd.read_parquet(out / "excluded_census_blocks.parquet")
    assert set(excluded["geoid20"]) == {"B", "C"}
    assert dict(zip(excluded["geoid20"], excluded["block_class"])) == {"B": "outside", "C": "water"}
    for col in ("geoid20", "pop20", "aland20", "awater20", "block_class"):
        assert col in excluded.columns


def test_census_run_writes_no_excluded_file_when_nothing_excluded(tmp_path: Path) -> None:
    """When every block is inside land, no excluded parquet is written (clean skip)."""
    boundary = gpd.GeoDataFrame(geometry=[box(-1, -1, 1, 1)], crs=4326)  # huge → all inside
    blocks = gpd.GeoDataFrame(
        {
            "geoid20": ["A"],
            "pop20": [10],
            "aland20": [100],
            "awater20": [0],
            "geometry": [box(0.01, 0.01, 0.02, 0.02)],
        },
        crs=4326,
    )
    census_path = tmp_path / "census.parquet"
    boundary_path = tmp_path / "boundary.geojson"
    blocks.to_parquet(census_path)
    boundary.to_file(boundary_path, driver="GeoJSON")
    out = tmp_path / "census_out"
    census._run(
        {"dataset:census": census_path, "dataset:analysis_boundary": boundary_path},
        out,
        BNAConfig.with_defaults(),
    )
    assert (out / "census_blocks.parquet").exists()
    assert not (out / "excluded_census_blocks.parquet").exists()


# ── export targets: excluded_blocks + analysis_boundary ───────────────────────


def _ctx(stage_dirs: dict[str, Path], dataset_paths: dict[str, Path]) -> ExportContext:
    from bikescore_bna.city import CityIdentity

    city = CityIdentity(name="Test", slug="test", region=None, country="united states")
    return ExportContext(
        stage_dirs=stage_dirs,
        dataset_paths=dataset_paths,
        city=city,
        config=BNAConfig.with_defaults(),
    )


def test_export_targets_are_registered() -> None:
    targets = list_export_targets()
    assert "excluded_blocks" in targets
    assert "analysis_boundary" in targets


def test_excluded_blocks_target_present_and_absent(tmp_path: Path) -> None:
    census_dir = tmp_path / "census"
    census_dir.mkdir()
    build = _EXPORT_TARGETS["excluded_blocks"].build

    # Absent → skipped cleanly (StageOutputNotFoundError, so bundles drop it).
    with pytest.raises(StageOutputNotFoundError):
        build(_ctx({"census": census_dir}, {}))

    # Present → returns the excluded layer with codes.
    _, boundary = _census_fixture()
    blocks, _ = _census_fixture()
    excluded = census.classify_census_blocks(blocks, boundary, 0.50, True)
    excluded = excluded[excluded["block_class"] != "included"]
    excluded.to_parquet(census_dir / "excluded_census_blocks.parquet")
    gdf = build(_ctx({"census": census_dir}, {}))
    assert "block_class" in gdf.columns
    assert set(gdf["geoid20"]) == {"B", "C"}


def test_analysis_boundary_target_skips_when_identical(tmp_path: Path) -> None:
    boundary = gpd.GeoDataFrame(geometry=[box(0, 0, 0.1, 0.1)], crs=4326)
    path = tmp_path / "boundary.geojson"
    boundary.to_file(path, driver="GeoJSON")
    build = _EXPORT_TARGETS["analysis_boundary"].build

    # Same file for both roles (identity acquisition) → skip.
    with pytest.raises(StageOutputNotFoundError):
        build(_ctx({}, {"analysis_boundary": path, "boundary": path}))

    # Distinct files but geometrically equal → still skip.
    path2 = tmp_path / "boundary2.geojson"
    boundary.to_file(path2, driver="GeoJSON")
    with pytest.raises(StageOutputNotFoundError):
        build(_ctx({}, {"analysis_boundary": path2, "boundary": path}))


def test_analysis_boundary_target_emits_when_different(tmp_path: Path) -> None:
    original = gpd.GeoDataFrame(geometry=[box(0, 0, 0.1, 0.1)], crs=4326)
    analysis = gpd.GeoDataFrame(geometry=[box(0, 0, 0.05, 0.05)], crs=4326)  # clipped
    op = tmp_path / "boundary.geojson"
    ap = tmp_path / "analysis.geojson"
    original.to_file(op, driver="GeoJSON")
    analysis.to_file(ap, driver="GeoJSON")

    build = _EXPORT_TARGETS["analysis_boundary"].build
    gdf = build(_ctx({}, {"analysis_boundary": ap, "boundary": op}))
    assert not gdf.empty
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
