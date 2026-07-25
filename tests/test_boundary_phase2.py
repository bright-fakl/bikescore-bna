"""Phase 2 boundary-manipulation tests: expanding transforms, multi-region acquire,
network buffer + pyosmium polygon clip, and the advisory coverage detector.

Self-contained: crafted geometries, tiny synthetic PBFs, and monkeypatched network I/O
(no real Geofabrik / pygris / LODES downloads). The osmium-CLI merge test skips when the
binary is absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

import bikescore_bna.acquire as acquire
from bikescore_bna import region_coverage as rc
from bikescore_bna.boundary import (
    has_boundary_transforms,
    load_override_geometry,
    prepare_boundary,
)
from bikescore_bna.city import CityIdentity
from bikescore_bna.config import BNAConfig, BoundaryConfig, ConfigValidationError
from bikescore_bna.config_resolver import build_config

_SRID = 32618  # UTM 18N — deterministic metric CRS for the fixtures below.


def _cfg(**boundary_kw: object) -> BNAConfig:
    c = BNAConfig.with_defaults()
    c.output_srid = _SRID
    c.boundary = BoundaryConfig(**boundary_kw)
    return c


def _multi_with_hole_and_island() -> gpd.GeoDataFrame:
    outer = Polygon([(0, 0), (0, 0.1), (0.1, 0.1), (0.1, 0)])
    hole = Polygon([(0.02, 0.02), (0.02, 0.04), (0.04, 0.04), (0.04, 0.02)])
    island = Polygon([(0.2, 0.2), (0.2, 0.21), (0.21, 0.21), (0.21, 0.2)])
    return gpd.GeoDataFrame(geometry=[MultiPolygon([outer.difference(hole), island])], crs=4326)


# ── convex_hull transform ─────────────────────────────────────────────────────


def test_has_boundary_transforms_includes_convex_hull() -> None:
    assert has_boundary_transforms(BoundaryConfig(convex_hull=True)) is True
    # override_geometry is a source, not a transform → not counted.
    assert has_boundary_transforms(BoundaryConfig(override_geometry=[0, 0, 1, 1])) is False
    # a bare non-zero network buffer is not a prepare_boundary transform either.
    assert has_boundary_transforms(BoundaryConfig(network_buffer_m=500)) is False


def test_convex_hull_drops_concavity_holes_and_islands() -> None:
    src = _multi_with_hole_and_island()
    result = prepare_boundary(src, _cfg(convex_hull=True))
    geom = result.geometry.iloc[0]
    assert geom.geom_type == "Polygon"
    assert len(geom.interiors) == 0
    assert geom.equals(geom.convex_hull)
    # The hull is expanding: it covers the detached island the original spans.
    assert geom.contains(Polygon([(0.2, 0.2), (0.2, 0.21), (0.21, 0.21), (0.21, 0.2)]).centroid)


# ── override_geometry source ──────────────────────────────────────────────────


def test_load_override_geometry_bbox() -> None:
    gdf = load_override_geometry([0.0, 0.0, 1.0, 2.0])
    assert gdf.crs.to_epsg() == 4326
    assert gdf.geometry.iloc[0].equals(box(0.0, 0.0, 1.0, 2.0))


def test_load_override_geometry_file(tmp_path: Path) -> None:
    p = tmp_path / "override.geojson"
    gpd.GeoDataFrame(geometry=[box(0, 0, 3, 3)], crs=4326).to_file(p, driver="GeoJSON")
    gdf = load_override_geometry(str(p))
    assert gdf.crs.to_epsg() == 4326
    assert gdf.geometry.iloc[0].bounds == (0.0, 0.0, 3.0, 3.0)


# ── config validation + resolver wiring ───────────────────────────────────────


def test_network_buffer_must_be_non_negative() -> None:
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(network_buffer_m=-1).validate()
    BoundaryConfig(network_buffer_m=0.0).validate()
    BoundaryConfig(network_buffer_m=2680).validate()


def test_override_geometry_bbox_validation() -> None:
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(override_geometry=[0, 0, 1]).validate()  # wrong length
    with pytest.raises(ConfigValidationError):
        BoundaryConfig(override_geometry=[1, 1, 0, 0]).validate()  # min >= max
    BoundaryConfig(override_geometry=[0, 0, 1, 1]).validate()  # ok
    BoundaryConfig(override_geometry="somewhere.geojson").validate()  # path: no fs check


def test_phase2_boundary_namespace_resolves() -> None:
    c = build_config(
        "default",
        {"boundary.convex_hull": True, "boundary.network_buffer_m": 1000},
    )
    assert c.boundary.convex_hull is True
    assert c.boundary.network_buffer_m == 1000


def test_extra_regions_resolves_from_scenario_globals() -> None:
    doc = {"type": "complete", "config": {"globals": {"extra_regions": ["maryland", "virginia"]}}}
    c = build_config(doc)
    assert c.extra_regions == ["maryland", "virginia"]


def test_extra_regions_set_inline_list() -> None:
    """--set is config-only (namespace whitelist), so a list-valued config field like
    extra_regions is settable inline via a bracketed value."""
    c = build_config("default", {"extra_regions": "[maryland, virginia]"})
    assert c.extra_regions == ["maryland", "virginia"]
    c2 = build_config("default", {"extra_regions": '["district-of-columbia"]'})
    assert c2.extra_regions == ["district-of-columbia"]


def test_default_config_has_empty_extra_regions_and_zero_buffer() -> None:
    c = build_config("default")
    assert c.extra_regions == []
    assert c.boundary.network_buffer_m == 0.0
    assert c.boundary.convex_hull is False
    assert c.boundary.override_geometry is None


# ── US state crosswalk ────────────────────────────────────────────────────────


def test_normalize_region_name_or_slug() -> None:
    assert rc.normalize_region("District of Columbia") == "district-of-columbia"
    assert rc.normalize_region("maryland") == "maryland"
    assert rc.normalize_region("Virginia") == "virginia"
    assert rc.normalize_region("new york") == "new-york"


def test_slug_crosswalk() -> None:
    assert rc.slug_to_fips("maryland") == "24"
    assert rc.slug_to_fips("district-of-columbia") == "11"
    assert rc.slug_to_fips("virginia") == "51"
    assert rc.slug_to_fips("narnia") is None
    assert rc.slug_to_abbr("virginia") == "va"
    assert rc.slug_to_abbr("district-of-columbia") == "dc"
    assert rc.slug_to_abbr("narnia") is None


def test_resolve_acquire_regions_home_first_deduped() -> None:
    city = CityIdentity(
        name="Washington", slug="dc", region="district of columbia",
        country="us", fips_code="1100000",
    )
    regions = rc.resolve_acquire_regions(city, ["maryland", "virginia", "district-of-columbia"])
    assert regions == ["district-of-columbia", "maryland", "virginia"]  # home first, dedup


# ── coverage detector (§9) ────────────────────────────────────────────────────


def _fake_index() -> gpd.GeoDataFrame:
    """DC + neighbours as adjacent unit boxes (id/parent shaped like the real index).

    Mirrors the real Geofabrik index: US state extracts are ``id="us/<slug>"`` with
    ``parent="north-america"`` (NOT ``parent="us"``) — the US marker is the id prefix.
    """
    return gpd.GeoDataFrame(
        {
            "id": ["us/district-of-columbia", "us/maryland", "us/virginia", "europe/france"],
            "parent": ["north-america", "north-america", "north-america", "europe"],
        },
        geometry=[
            box(0, 0, 1, 1),      # DC
            box(1, 0, 2, 1),      # Maryland — east
            box(0, -1, 1, 0),     # Virginia — south
            box(10, 10, 11, 11),  # unrelated non-US extract
        ],
        crs=4326,
    )


@pytest.fixture()
def _patched_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "load_geofabrik_index", lambda *a, **k: _fake_index())


def _analysis(geom: Polygon) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[geom], crs=4326)


def test_coverage_interior_extent_passes(_patched_index: None) -> None:
    interior = _analysis(box(0.2, 0.2, 0.8, 0.8))  # wholly within DC
    rc.check_coverage(interior, 0.0, ["district-of-columbia"])  # no raise


def test_coverage_extent_crossing_into_maryland_raises(_patched_index: None) -> None:
    crossing = _analysis(box(0.6, 0.2, 1.4, 0.8))  # spans DC and Maryland
    with pytest.raises(rc.RegionCoverageError, match="maryland"):
        rc.check_coverage(crossing, 0.0, ["district-of-columbia"])


def test_coverage_extent_covered_when_neighbour_acquired(_patched_index: None) -> None:
    crossing = _analysis(box(0.6, 0.2, 1.4, 0.8))
    rc.check_coverage(crossing, 0.0, ["district-of-columbia", "maryland"])  # no raise


def test_coverage_network_buffer_pushes_across_line(_patched_index: None) -> None:
    interior = _analysis(box(0.2, 0.2, 0.95, 0.8))  # inside DC, hugs the eastern edge
    # No buffer: covered by DC alone.
    rc.check_coverage(interior, 0.0, ["district-of-columbia"])
    # A large buffer dilates east past x=1 into Maryland → must flag it.
    with pytest.raises(rc.RegionCoverageError, match="maryland"):
        rc.check_coverage(interior, 30000.0, ["district-of-columbia"])


def test_missing_regions_difference_based(_patched_index: None) -> None:
    interior = _analysis(box(0.2, 0.2, 0.8, 0.8))  # within DC → nothing extra needed
    assert rc.missing_regions(interior, 0.0, ["district-of-columbia"]) == []
    crossing = _analysis(box(0.6, 0.2, 1.4, 0.8))  # spills into Maryland
    assert rc.missing_regions(crossing, 0.0, ["district-of-columbia"]) == ["maryland"]
    # Acquiring the neighbour closes the gap.
    assert rc.missing_regions(crossing, 0.0, ["district-of-columbia", "maryland"]) == []


def test_plan_acquire_regions_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "load_geofabrik_index", lambda *a, **k: _fake_index())
    city = CityIdentity(
        name="Washington", slug="dc", region="district of columbia",
        country="us", fips_code="1100000",
    )
    # override_geometry avoids a boundary network fetch; the box straddles DC→Maryland.
    cfg = BNAConfig.with_defaults()
    cfg.boundary = BoundaryConfig(override_geometry=[0.6, 0.2, 1.4, 0.8])

    plan = acquire.plan_acquire_regions(city, cfg)
    assert plan.home_region == "district-of-columbia"
    assert plan.acquire_regions == ["district-of-columbia"]  # no extra_regions yet
    assert plan.missing_regions == ["maryland"]
    assert plan.needed_regions == ["district-of-columbia", "maryland"]

    # Adding the neighbour clears the gap.
    cfg.extra_regions = ["maryland"]
    plan2 = acquire.plan_acquire_regions(city, cfg)
    assert plan2.missing_regions == []
    assert plan2.needed_regions == ["district-of-columbia", "maryland"]
    assert plan2.acquire_regions == ["district-of-columbia", "maryland"]


def test_plan_acquire_regions_rejects_negative_buffer() -> None:
    """The acquire/dry-run path validates the boundary config before any network I/O."""
    city = CityIdentity(
        name="Rockville", slug="rockville", region="maryland",
        country="us", fips_code="2400000",
    )
    cfg = BNAConfig.with_defaults()
    # override_geometry means no boundary fetch even if validation were skipped.
    cfg.boundary = BoundaryConfig(override_geometry=[0, 0, 1, 1], network_buffer_m=-100)
    with pytest.raises(ConfigValidationError, match="network_buffer_m"):
        acquire.plan_acquire_regions(city, cfg)


def test_coverage_offline_index_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError("network down")

    monkeypatch.setattr(rc, "load_geofabrik_index", _boom)
    # Advisory guard: an unreachable index must not crash acquire.
    rc.check_coverage(_analysis(box(0.6, 0.2, 1.4, 0.8)), 0.0, ["district-of-columbia"])


# ── OSM merge (osmium CLI) ────────────────────────────────────────────────────


def _write_pbf(path: Path, nodes: dict[int, tuple[float, float]], ways: list[tuple[int, list[int]]]) -> None:
    import osmium
    from osmium.osm import mutable

    w = osmium.SimpleWriter(str(path))
    try:
        for nid in sorted(nodes):
            lon, lat = nodes[nid]
            w.add_node(mutable.Node(id=nid, location=(lon, lat), tags={}))
        for wid, refs in sorted(ways):
            w.add_way(mutable.Way(id=wid, nodes=refs, tags={"highway": "residential"}))
    finally:
        w.close()


@pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium CLI required for merge")
def test_osmium_merge_stitches_seam_and_dedups_border_way(tmp_path: Path) -> None:
    import osmium

    # Two reference-complete extracts sharing border way 100 (nodes 1-2) in full.
    # A also has way 10 (2-3); B also has way 20 (2-4). Node 2 is the seam.
    a = tmp_path / "a.osm.pbf"
    b = tmp_path / "b.osm.pbf"
    _write_pbf(a, {1: (0.0, 0.0), 2: (0.1, 0.0), 3: (0.2, 0.0)}, [(10, [2, 3]), (100, [1, 2])])
    _write_pbf(b, {1: (0.0, 0.0), 2: (0.1, 0.0), 4: (0.1, 0.1)}, [(20, [2, 4]), (100, [1, 2])])

    merged = acquire._osmium_merge([a, b], tmp_path / "merged.osm.pbf")

    way_ids = sorted(w.id for w in osmium.FileProcessor(str(merged), osmium.osm.WAY))
    assert way_ids == [10, 20, 100]  # border way 100 deduped to a single connected way

    # Node 2 ties both sides together: a route 3-2-1 (A) and 4-2 (B) exists across the seam.
    ways = {w.id: [n.ref for n in w.nodes] for w in osmium.FileProcessor(str(merged), osmium.osm.WAY)}
    assert 2 in ways[10] and 2 in ways[20] and 2 in ways[100]


def test_osmium_merge_requires_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(acquire.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="osmium"):
        acquire._osmium_merge([tmp_path / "a.pbf", tmp_path / "b.pbf"], tmp_path / "m.pbf")


# ── pyosmium polygon clip (bbox → polygon fix) ────────────────────────────────


def test_pyosmium_clip_uses_polygon_not_bbox(tmp_path: Path) -> None:
    from bikescore_bna.stages.parse import _clip_with_pyosmium

    # Right-triangle boundary; its bbox is (0,0)-(2,2). A way in the bbox *corner*
    # (1.5,1.5)-(1.6,1.6) lies OUTSIDE the triangle (x+y > 2) — the old bbox filter would
    # wrongly keep it; the polygon test must drop it.
    triangle = Polygon([(0, 0), (2, 0), (0, 2)])
    src = tmp_path / "src.osm.pbf"
    _write_pbf(
        src,
        {1: (0.3, 0.3), 2: (0.4, 0.4), 3: (1.5, 1.5), 4: (1.6, 1.6)},
        [(11, [1, 2]), (99, [3, 4])],  # 11 inside triangle, 99 in bbox corner outside
    )

    out = tmp_path / "clipped.osm.pbf"
    _clip_with_pyosmium(src, triangle, out)

    import osmium

    way_ids = sorted(w.id for w in osmium.FileProcessor(str(out), osmium.osm.WAY))
    assert way_ids == [11]  # corner way 99 excluded by the polygon test


# ── multi-state census + LODES concat (row concat, GEOID dedup) ────────────────


def test_census_multi_state_concat_and_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pygris

    def fake_blocks(state: str, year: int, cache: bool) -> gpd.GeoDataFrame:
        rows = {
            "24": [("24A", box(0.1, 0.1, 0.2, 0.2)), ("DUP", box(0.5, 0.5, 0.6, 0.6))],
            "51": [("51B", box(0.3, 0.3, 0.4, 0.4)), ("DUP", box(0.5, 0.5, 0.6, 0.6))],
        }[state]
        return gpd.GeoDataFrame(
            {"GEOID20": [r[0] for r in rows]},
            geometry=[r[1] for r in rows],
            crs=4326,
        )

    monkeypatch.setattr(pygris, "blocks", fake_blocks)
    boundary = gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs=4326)

    out = acquire._download_census_blocks_tmp(["24", "51"], boundary, tmp_path)
    assert out is not None
    g = gpd.read_parquet(out)
    # Both states concatenated; the shared "DUP" geoid appears once.
    assert sorted(g["geoid20"]) == ["24A", "51B", "DUP"]


def test_lodes_multi_state_concat_and_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquire, "_lodes_latest_year", lambda abbr, base: 2021)

    def fake_download(url: str, dest: Path) -> Path | None:
        if "_od_main_" not in url:
            return None  # no aux available
        if "/md/" in url:
            dest.write_text("w_geocode,h_geocode,S000\n240010001,240010002,5\nSHARED,SHARED,1\n")
        elif "/va/" in url:
            dest.write_text("w_geocode,h_geocode,S000\n510010001,510010002,7\nSHARED,SHARED,1\n")
        else:
            return None
        return dest

    monkeypatch.setattr(acquire, "_download_lodes_file_tmp", fake_download)

    main, aux = acquire._download_lodes_multi(["md", "va"], tmp_path)
    assert aux is None
    assert main is not None
    df = pd.read_csv(main, dtype=str)
    # Two state rows + a single deduped SHARED OD pair.
    assert len(df) == 3
    assert sorted(df["w_geocode"]) == ["240010001", "510010001", "SHARED"]
