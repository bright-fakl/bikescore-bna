"""Destinations stage: cluster POIs and associate with census blocks.

SQL equivalent: connectivity/destinations/*.sql (13 files)

Each destination type defined in config.destinations.active() is processed
independently: POIs are filtered from poi_raw, clustered spatially, and then
associated with census blocks via geometric intersection.

Four clustering modes match the distinct SQL patterns across the 13 standard types:
- poly_cluster  — cluster polygons; standalone points outside clusters (colleges, etc.)
- no_cluster    — each polygon is its own record; remove sub-polygons (schools, etc.)
- retail        — cluster all (polygons + 10m-buffered points) together
- transit       — individual polygon records; cluster points excluding near-poly ones
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore.config import BNAConfig
    from bikescore.stages.parse import RawPOI


# ── Geometry helpers ──────────────────────────────────────────────────────────

_logger = logging.getLogger("bikescore")

_LARGE_DIST = 1e15  # sentinel for NaN distances; larger than any city-scale distance


def _pairwise_dist(geoms: list) -> np.ndarray:
    """Pairwise exterior-to-exterior distances between geometries.

    NaN distances (from empty or degenerate geometry) are replaced with
    _LARGE_DIST so scipy's squareform/linkage don't reject the matrix.
    Geometries with NaN distances will never merge at any real clustering
    threshold and are returned as singleton clusters.
    """
    n = len(geoms)
    mat = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = geoms[i].distance(geoms[j])
            if d != d:  # NaN check: NaN is the only value not equal to itself
                d = _LARGE_DIST
            mat[i, j] = d
            mat[j, i] = d
    return mat


def _dbscan_labels(geoms: list, eps: float) -> np.ndarray:
    """Cluster geometries using pairwise distances and single-linkage hierarchical clustering.

    Equivalent to DBSCAN with min_samples=1 on a precomputed distance matrix —
    all geometries within eps of any cluster member join the cluster.
    Uses scipy single-linkage (already a dependency) instead of sklearn.
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    n = len(geoms)
    if n == 0:
        return np.array([], dtype=int)
    if n == 1:
        return np.array([0])
    dist = _pairwise_dist(geoms)
    Z = linkage(squareform(dist), method="single")
    return fcluster(Z, t=eps, criterion="distance") - 1  # 0-based labels


def _remove_contained(polys: list) -> list:
    """Remove polygons fully contained within another polygon in the list."""
    if len(polys) <= 1:
        return list(polys)
    tree = STRtree(polys)
    keep = []
    for i, poly in enumerate(polys):
        dominated = False
        for j in tree.query(poly):
            if j != i and polys[j].contains(poly):
                dominated = True
                break
        if not dominated:
            keep.append(poly)
    return keep


def _points_outside_polys(pts: list, polys: list) -> list:
    """Return points not intersecting any polygon in polys."""
    if not polys:
        return list(pts)
    tree = STRtree(polys)
    result = []
    for pt in pts:
        if not any(polys[j].intersects(pt) for j in tree.query(pt)):
            result.append(pt)
    return result


# ── Clustering algorithms ─────────────────────────────────────────────────────

def _cluster_poly_cluster(
    poly_geoms: list,
    pt_geoms: list,
    eps: float,
    pt_primary: list[bool] | None = None,
) -> list[tuple]:
    """poly_cluster mode: cluster polygons, add standalone points outside clusters.

    Returns list of (geom_poly, geom_pt) tuples.
    geom_poly is None for standalone point records.

    pt_primary: optional boolean list, same length as pt_geoms. When provided,
        points marked primary (True) are always included even if inside a polygon
        cluster — replicating the SQL AND/OR precedence behavior where the first
        WHERE condition does not carry the NOT EXISTS exclusion.
    """
    records = []

    if poly_geoms:
        labels = _dbscan_labels(poly_geoms, eps)
        n_clusters = int(labels.max()) + 1
        for label in range(n_clusters):
            members = [poly_geoms[i] for i in range(len(poly_geoms)) if labels[i] == label]
            merged = unary_union(members)
            centroid = merged.centroid
            records.append((merged, centroid))

    cluster_polys = [r[0] for r in records]

    if pt_primary is None:
        for pt in _points_outside_polys(pt_geoms, cluster_polys):
            records.append((None, pt))
    else:
        # Primary points: always include
        primary_pts = [pt for pt, p in zip(pt_geoms, pt_primary) if p]
        secondary_pts = [pt for pt, p in zip(pt_geoms, pt_primary) if not p]
        for pt in primary_pts:
            records.append((None, pt))
        for pt in _points_outside_polys(secondary_pts, cluster_polys):
            records.append((None, pt))

    return records


def _cluster_no_cluster(
    poly_geoms: list,
    pt_geoms: list,
) -> list[tuple]:
    """no_cluster mode: each polygon is its own record; remove sub-polygons."""
    records = []

    filtered = _remove_contained(poly_geoms)
    for poly in filtered:
        centroid = poly.centroid
        records.append((poly, centroid))

    for pt in _points_outside_polys(pt_geoms, filtered):
        records.append((None, pt))

    return records


def _cluster_retail(
    poly_geoms: list,
    pt_geoms: list,
    eps: float,
) -> list[tuple]:
    """retail mode: cluster all (polygons + 10m-buffered points) together.

    Returns list of (geom_poly, geom_pt) tuples; all records have geom_poly.
    """
    all_geoms = list(poly_geoms) + [p.buffer(10) for p in pt_geoms]
    if not all_geoms:
        return []

    labels = _dbscan_labels(all_geoms, eps)
    n_clusters = int(labels.max()) + 1
    records = []
    for label in range(n_clusters):
        members = [all_geoms[i] for i in range(len(all_geoms)) if labels[i] == label]
        merged = unary_union(members)
        centroid = merged.centroid
        records.append((merged, centroid))
    return records


def _cluster_transit(
    poly_geoms: list,
    pt_geoms: list,
    eps: float,
) -> list[tuple]:
    """transit mode: individual polygon records; cluster points excluding near-poly ones.

    Returns list of (geom_poly, geom_pt) tuples.
    geom_poly is None for clustered point records.
    """
    records = []

    filtered_polys = _remove_contained(poly_geoms)
    for poly in filtered_polys:
        centroid = poly.centroid
        records.append((poly, centroid))

    # Exclude points within eps of any polygon
    eligible_pts = pt_geoms
    if filtered_polys:
        tree = STRtree(filtered_polys)
        eligible_pts = [
            pt for pt in pt_geoms
            if not any(filtered_polys[j].distance(pt) < eps for j in tree.query(pt.buffer(eps)))
        ]

    if eligible_pts:
        labels = _dbscan_labels(eligible_pts, eps)
        n_clusters = int(labels.max()) + 1
        for label in range(n_clusters):
            members = [eligible_pts[i] for i in range(len(eligible_pts)) if labels[i] == label]
            centroid = unary_union(members).centroid
            records.append((None, centroid))

    return records


# ── Block association ─────────────────────────────────────────────────────────

def _associate_blocks(
    records: list[tuple],
    blocks_proj: gpd.GeoDataFrame,
) -> list[list[str]]:
    """Find census block GEOIDs that intersect each destination record.

    For records with geom_poly: check intersection with the polygon.
    For point-only records (geom_poly is None): check if geom_pt intersects a block.
    Returns list of lists of GEOID20 strings.
    """
    block_geoids = blocks_proj["geoid20"].astype(str).tolist()
    block_polys = list(blocks_proj.geometry)
    block_tree = STRtree(block_polys)

    result = []
    for geom_poly, geom_pt in records:
        # Query geometry: polygon takes priority (its centroid is usually inside)
        query_geom = geom_poly if geom_poly is not None else geom_pt
        candidate_idxs = block_tree.query(query_geom)
        matched = [
            block_geoids[j]
            for j in candidate_idxs
            if block_polys[j].intersects(query_geom)
        ]
        # For poly records, also check centroid in case centroid is in a different block
        if geom_poly is not None:
            for j in block_tree.query(geom_pt):
                g = block_geoids[j]
                if g not in matched and block_polys[j].intersects(geom_pt):
                    matched.append(g)
        result.append(matched)

    return result


# ── Main stage function ───────────────────────────────────────────────────────

def compute_destinations(
    poi_raw: list[RawPOI],
    blocks_df: gpd.GeoDataFrame,
    config: BNAConfig,
    boundary: gpd.GeoDataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Cluster POIs by destination type and associate clusters with census blocks.

    Args:
        poi_raw: Raw POI list from parse(). Each RawPOI has geometry (Point or
            Polygon in WGS84), matched_types (list of destination type names that
            matched at parse time), and tags (for exclude matcher evaluation).
        blocks_df: Census block GeoDataFrame with geoid20, geometry in EPSG:4326.
        config: Pipeline configuration. Uses config.destinations registry.
        boundary: Optional city boundary GeoDataFrame (EPSG:4326). When provided,
            POIs are pre-clipped to this boundary before clustering. This matches
            the SQL reference which only scans neighborhood_osm_full_polygon/point
            (city boundary only, not the routing buffer zone). If None, all poi_raw
            entries are used as-is.

    Returns:
        Mapping destination_name → DataFrame with columns:
            id            — 1-based cluster ID
            blockid20     — list of GEOID20s of blocks containing this cluster
            geom_pt       — cluster centroid as shapely Point (projected CRS)
            geom_poly     — cluster polygon as shapely geometry (None for point records)
            pop_low_stress  — 0 (placeholder; filled by scores stage)
            pop_high_stress — 0 (placeholder; filled by scores stage)
            pop_score       — 0.0 (placeholder; filled by scores stage)
    """
    from bikescore.destinations import default_destination_registry

    registry = config.destinations
    if registry is None:
        registry = default_destination_registry()

    _n_poi_raw = len(poi_raw)
    _logger.info("destinations  input — poi_raw=%d", _n_poi_raw)

    # Clip POIs to match brokenspoke-analyzer's data scope.
    #
    # Polygon POIs: intersects(exact_boundary)
    #   osmium complete_ways keeps polygon ways that cross the city boundary (some
    #   nodes inside, some outside). These ways appear in neighborhood_osm_full_polygon
    #   and affect clustering. Use intersects to match that scope.
    #
    # Point POIs: DWithin(max_trip_distance)
    #   clip_osm.sql applies ST_DWithin(point, boundary, nb_boundary_buffer) to
    #   neighborhood_osm_full_point. Points referenced by ways crossing the boundary
    #   (e.g. ferry terminals just outside the city) are kept by osmium and survive
    #   the DWithin filter. The bbox clip in brokenspoke-analyzer that accidentally
    #   excludes some of these is a relic; we implement the intent instead.
    if boundary is not None:
        import geopandas as gpd
        from shapely.ops import unary_union
        buffer_m = float(config.max_trip_distance)
        boundary_4326 = boundary.to_crs("EPSG:4326")
        boundary_geom = unary_union(boundary_4326.geometry)
        utm_crs = boundary_4326.estimate_utm_crs()
        boundary_buffered = gpd.GeoSeries(
            [unary_union(boundary_4326.to_crs(utm_crs).buffer(buffer_m))],
            crs=utm_crs,
        ).to_crs("EPSG:4326").iloc[0]
        clipped: list = []
        for poi in poi_raw:
            if isinstance(poi.geometry, Polygon):
                if boundary_geom.intersects(poi.geometry):
                    clipped.append(poi)
            elif isinstance(poi.geometry, Point):
                if boundary_buffered.intersects(poi.geometry):
                    clipped.append(poi)
            else:
                if boundary_geom.intersects(poi.geometry):
                    clipped.append(poi)
        poi_raw = clipped
        _logger.info("destinations  boundary clip — %d → %d POIs", _n_poi_raw, len(poi_raw))

    # network_path entries (trails) are not POIs — scored separately in the scores
    # stage from their network detection, so they are skipped here.
    dest_types = [dt for dt in registry.active() if dt.type != "network_path"]
    _logger.info("destinations  dest_types=%d", len(dest_types))
    if not dest_types:
        return {}

    # Project blocks to UTM for metric operations
    blocks_4326 = blocks_df if blocks_df.crs and str(blocks_df.crs).startswith("EPSG:4326") \
        else blocks_df.to_crs("EPSG:4326")
    utm_crs = blocks_4326.geometry.dropna().iloc[:1].estimate_utm_crs() \
        if not blocks_4326.geometry.dropna().empty else None

    if utm_crs is None:
        return {dt.name: pd.DataFrame() for dt in dest_types}

    blocks_proj = blocks_4326.to_crs(utm_crs)

    import geopandas as gpd
    from pyproj import Transformer

    wgs84_to_utm = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)

    def _project_point(pt: Point) -> Point:
        x, y = wgs84_to_utm.transform(pt.x, pt.y)
        return Point(x, y)

    def _project_poly(poly: Polygon) -> Polygon:
        exterior = [wgs84_to_utm.transform(x, y) for x, y in poly.exterior.coords]
        interiors = [[wgs84_to_utm.transform(x, y) for x, y in ring.coords]
                     for ring in poly.interiors]
        proj = Polygon(exterior, interiors)
        if not proj.is_valid:
            proj = proj.buffer(0)
        return proj


    results: dict[str, pd.DataFrame] = {}

    for dest_type in dest_types:
        poly_geoms_proj: list = []
        pt_geoms_proj: list = []
        pt_primary_flags: list[bool] = []  # True = primary matcher → always include

        # Primary matcher = first node_matcher (if multiple exist).
        # The SQL reference has an AND/OR precedence quirk: WHERE cond1 OR (cond2 AND NOT EXISTS(...))
        # means cond1 points are always included even inside polygon clusters.
        node_rows = dest_type.node_match.rows
        first_node_matcher = node_rows[0] if node_rows else None
        has_secondary_matchers = len(node_rows) > 1

        for poi in poi_raw:
            if dest_type.name not in poi.matched_types:
                continue
            # Apply exclude matcher
            if dest_type.exclude_match.matches(poi.tags):
                continue

            if isinstance(poi.geometry, Polygon):
                proj = _project_poly(poi.geometry)
                if proj.is_valid and not proj.is_empty:
                    poly_geoms_proj.append(proj)
            elif isinstance(poi.geometry, Point):
                pt_geoms_proj.append(_project_point(poi.geometry))
                # Primary = matches first node_matcher (always included even inside polygon clusters)
                is_primary = (
                    has_secondary_matchers
                    and first_node_matcher is not None
                    and first_node_matcher.matches(poi.tags)
                )
                pt_primary_flags.append(is_primary)

        eps = float(dest_type.clustering_tolerance_m)
        mode = dest_type.clustering_mode

        # Pass primary flags only when there are secondary matchers (otherwise all equivalent)
        primary_arg = pt_primary_flags if has_secondary_matchers else None

        if mode == "poly_cluster":
            records = _cluster_poly_cluster(poly_geoms_proj, pt_geoms_proj, eps, primary_arg)
        elif mode == "no_cluster":
            records = _cluster_no_cluster(poly_geoms_proj, pt_geoms_proj)
        elif mode == "retail":
            records = _cluster_retail(poly_geoms_proj, pt_geoms_proj, eps)
        elif mode == "transit":
            records = _cluster_transit(poly_geoms_proj, pt_geoms_proj, eps)
        else:
            # Unknown mode: fall back to poly_cluster
            records = _cluster_poly_cluster(poly_geoms_proj, pt_geoms_proj, eps, primary_arg)

        block_ids = _associate_blocks(records, blocks_proj)

        rows = []
        for i, ((geom_poly, geom_pt), bids) in enumerate(zip(records, block_ids), start=1):
            rows.append({
                "id": i,
                "blockid20": bids,
                "geom_pt": geom_pt,
                "geom_poly": geom_poly,
                "pop_low_stress": 0,
                "pop_high_stress": 0,
                "pop_score": 0.0,
            })

        df = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["id", "blockid20", "geom_pt", "geom_poly",
                     "pop_low_stress", "pop_high_stress", "pop_score"]
        )
        results[dest_type.name] = df

        _logger.info(
            "destinations  %s — clusters=%d  polys=%d  pts=%d",
            dest_type.name, len(df), len(poly_geoms_proj), len(pt_geoms_proj),
        )

    _logger.info(
        "destinations  output — types=%d  total_clusters=%d",
        len(results), sum(len(v) for v in results.values()),
    )
    return results


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

import pickle  # noqa: E402
from pathlib import Path  # noqa: E402

from bikescore.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd
    from shapely import wkb as swkb

    parse_dir = Path(input_paths["parse"])
    census_dir = Path(input_paths["census"])

    with open(parse_dir / "poi_raw.pkl", "rb") as f:
        poi_raw = pickle.load(f)
    blocks_gdf = gpd.read_parquet(census_dir / "census_blocks.parquet")

    results = compute_destinations(poi_raw, blocks_gdf, config)

    # geom_pt/geom_poly hold raw shapely geometries in a projected CRS;
    # encode as EWKB hex (with SRID) so plain pandas.to_parquet can write
    # them and downstream readers (export.py:_parse_geom_pt) can recover
    # both geometry and source CRS.
    blocks_4326 = blocks_gdf if blocks_gdf.crs and str(blocks_gdf.crs).startswith("EPSG:4326") \
        else blocks_gdf.to_crs("EPSG:4326")
    utm_crs = blocks_4326.geometry.dropna().iloc[:1].estimate_utm_crs() \
        if not blocks_4326.geometry.dropna().empty else None
    srid = utm_crs.to_epsg() if utm_crs is not None else None

    def _to_ewkb_hex(geom: object) -> str | None:
        if geom is None or (isinstance(geom, float) and pd.isna(geom)):
            return None
        return swkb.dumps(geom, hex=True, srid=srid, include_srid=srid is not None)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, df in results.items():
        df = df.copy()
        for col in ("geom_pt", "geom_poly"):
            if col in df.columns:
                df[col] = df[col].map(_to_ewkb_hex)
        df.to_parquet(out / f"dest_{name}.parquet")

    summary_rows = [
        {"dest_type": name, "cluster_count": len(df)}
        for name, df in results.items()
    ]
    pd.DataFrame(summary_rows).to_parquet(out / "destination_summary.parquet")


DESTINATIONS = StageSpec(
    name="destinations",
    depends_on=("parse", "census"),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
