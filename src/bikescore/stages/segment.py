"""Segment stage: topology splitting and trail cluster detection."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import unary_union

from bikescore.config import BNAConfig

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    from bikescore.decision.model import Matcher

_logger = logging.getLogger("bikescore")


@dataclass
class SegmentResult:
    """Output of the segment stage."""

    segments: gpd.GeoDataFrame
    orphan_osm_ids: frozenset[int]


# ── Topology splitting ────────────────────────────────────────────────────────

def _compute_node_usage(ways_df: gpd.GeoDataFrame) -> dict[int, int]:
    """Count how many ways each node appears in (mirrors osm2pgrouting numsOfUse)."""
    usage: dict[int, int] = defaultdict(int)
    for node_ids in ways_df["node_ids"]:
        if isinstance(node_ids, list):
            for nid in node_ids:
                usage[int(nid)] += 1
    return dict(usage)


def _detect_orphan_osm_ids(
    ways_df: gpd.GeoDataFrame,
    node_usage: dict[int, int],
) -> frozenset[int]:
    """Return osm_ids of ways that share no node with any other way.

    A way is an orphan if every node in its node_ids list appears in exactly
    one way (usage == 1). Mirrors the orphan DELETE in functional_class.sql.
    """
    orphans: set[int] = set()
    for _, row in ways_df.iterrows():
        nids = row.get("node_ids")
        if not isinstance(nids, list):
            continue
        if not any(node_usage.get(int(n), 0) > 1 for n in nids):
            osm_id = row.get("osm_id")
            if osm_id is not None:
                orphans.add(int(osm_id))
    return frozenset(orphans)


def _split_way(
    node_ids: list[int],
    coords: list[tuple[float, float]],
    node_usage: dict[int, int],
    features: dict,
    seg_id: int,
) -> tuple[list[dict], int]:
    """Split one OSM way into segments at intersection nodes.

    A node is an intersection if it appears in more than one way. Each resulting
    segment stores start_node_id, end_node_id, and a unique road_id = seg_id.
    road_id is a sequential integer unique per segment, matching brokenspoke's
    SERIAL road_id. end_node_id is kept as a separate field for topology only.
    Matches osm2pgrouting Way::split_me() behaviour.
    """
    segments: list[dict] = []
    cur_nodes: list[int] = []
    cur_coords: list[tuple] = []

    def flush() -> None:
        nonlocal seg_id
        if len(cur_nodes) < 2:
            return
        seg = dict(features)
        seg["segment_id"] = seg_id
        seg["start_node_id"] = cur_nodes[0]
        seg["end_node_id"] = cur_nodes[-1]
        seg["road_id"] = seg_id
        seg["geometry"] = LineString(cur_coords)
        segments.append(seg)
        seg_id += 1

    for i, nid in enumerate(node_ids):
        cur_nodes.append(nid)
        cur_coords.append(coords[i])
        if node_usage.get(int(nid), 0) > 1:
            flush()
            cur_nodes = [nid]
            cur_coords = [coords[i]]

    flush()
    return segments, seg_id


def split_ways(
    ways_df: gpd.GeoDataFrame,
    nodes_df: pd.DataFrame,
    all_ways_df: gpd.GeoDataFrame | None = None,
) -> gpd.GeoDataFrame:
    """Split all OSM ways at intersection nodes.

    Args:
        ways_df: GeoDataFrame with 'node_ids' (list[int]) and 'geometry'
            (shapely LineString in EPSG:4326). All other columns are propagated.
        nodes_df: DataFrame with 'node_id', 'lon', 'lat' columns.
        all_ways_df: Optional full set of ways (including unclassified) used
            solely to compute node_usage, so classified ways are also split at
            junctions with footways, service roads, etc. (matches osm2pgrouting).

    Returns:
        GeoDataFrame of segments in EPSG:4326. Columns include segment_id,
        start_node_id, end_node_id, road_id (unique sequential int = segment_id), geometry, plus
        all non-structural columns from ways_df.
    """
    node_coords: dict[int, tuple[float, float]] = {
        int(r["node_id"]): (float(r["lon"]), float(r["lat"]))
        for _, r in nodes_df.iterrows()
    }
    node_usage = _compute_node_usage(all_ways_df if all_ways_df is not None else ways_df)
    skip_cols = {"node_ids", "geometry", "tags"}
    feature_cols = [c for c in ways_df.columns if c not in skip_cols]

    all_segs: list[dict] = []
    seg_id = 0

    for _, row in ways_df.iterrows():
        node_ids = row.get("node_ids")
        # Parquet round-trips list columns as numpy arrays, not python lists.
        if isinstance(node_ids, np.ndarray):
            node_ids = node_ids.tolist()
        if not isinstance(node_ids, list) or len(node_ids) < 2:
            continue
        coords = [node_coords.get(int(nid)) for nid in node_ids]
        if None in coords:
            continue
        features = {c: row[c] for c in feature_cols}
        segs, seg_id = _split_way(node_ids, coords, node_usage, features, seg_id)
        all_segs.extend(segs)

    if not all_segs:
        return gpd.GeoDataFrame(
            columns=["segment_id", "start_node_id", "end_node_id", "road_id", "geometry"],
            crs="EPSG:4326",
        )

    return gpd.GeoDataFrame(all_segs, crs="EPSG:4326")


# ── Boundary split ────────────────────────────────────────────────────────────

def _split_line_by_polygon(
    line: LineString,
    polygon: object,
) -> list[LineString]:
    """Split a LineString at polygon boundary crossings, returning parts in original order."""
    from shapely.geometry import Point

    in_part = line.intersection(polygon)
    out_part = line.difference(polygon)

    parts: list = []
    for geom in (in_part, out_part):
        if geom is None or geom.is_empty:
            continue
        t = geom.geom_type
        if t == "LineString":
            if geom.length > 0:
                parts.append(geom)
        elif t in ("MultiLineString", "GeometryCollection"):
            for g in geom.geoms:
                if g.geom_type == "LineString" and g.length > 0:
                    parts.append(g)

    if len(parts) <= 1:
        return parts

    # Sort by parametric position of each part's first coordinate along the original line.
    parts.sort(key=lambda p: line.project(Point(p.coords[0])))
    return parts


def split_at_boundary(
    segments_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    virtual_id_start: int,
) -> tuple[gpd.GeoDataFrame, int]:
    """Split segments that cross the city boundary polygon.

    Crossing segments are replaced by sub-segments. Virtual node IDs (>=
    virtual_id_start) are assigned at boundary crossing points so the graph
    topology remains connected at those points.

    Returns updated GeoDataFrame (same CRS as input) and next available
    virtual node ID.
    """
    if len(segments_gdf) == 0:
        return segments_gdf, virtual_id_start

    src_crs = segments_gdf.crs
    utm_crs = segments_gdf.estimate_utm_crs()
    segs_utm = segments_gdf.to_crs(utm_crs)
    boundary_utm = unary_union(boundary_gdf.to_crs(utm_crs).geometry)

    virtual_counter = virtual_id_start
    id_counter = int(max(segments_gdf["road_id"].max(), segments_gdf["segment_id"].max())) + 1

    non_geom = [
        c for c in segments_gdf.columns
        if c not in {"geometry", "start_node_id", "end_node_id", "road_id", "segment_id"}
    ]

    drop_indices: list = []
    new_rows: list[dict] = []

    for idx, row in segs_utm.iterrows():
        seg_geom = row.geometry
        if boundary_utm.contains(seg_geom):
            continue
        if not boundary_utm.intersects(seg_geom):
            continue

        parts = _split_line_by_polygon(seg_geom, boundary_utm)
        if len(parts) <= 1:
            continue

        drop_indices.append(idx)

        n_cross = len(parts) - 1
        virtual_ids = list(range(virtual_counter, virtual_counter + n_cross))
        virtual_counter += n_cross

        start_ids = [int(row["start_node_id"]), *virtual_ids]
        end_ids = [*virtual_ids, int(row["end_node_id"])]
        base = {c: row[c] for c in non_geom}

        for part_geom, s_id, e_id in zip(parts, start_ids, end_ids):
            r = dict(base)
            r["start_node_id"] = s_id
            r["end_node_id"] = e_id
            r["road_id"] = id_counter
            r["segment_id"] = id_counter
            r["geometry"] = part_geom
            new_rows.append(r)
            id_counter += 1

    if not drop_indices:
        return segments_gdf, virtual_counter

    result_utm = segs_utm.drop(index=drop_indices).reset_index(drop=True)
    if new_rows:
        new_gdf = gpd.GeoDataFrame(new_rows, crs=utm_crs)
        result_utm = gpd.GeoDataFrame(
            pd.concat([result_utm, new_gdf], ignore_index=True), crs=utm_crs
        )

    _logger.info(
        "segment  split_at_boundary — %d crossing segs → %d sub-segments",
        len(drop_indices), len(new_rows),
    )
    return result_utm.to_crs(src_crs), virtual_counter


def remove_out_of_city_deadends(
    segments_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    virtual_id_start: int,
) -> gpd.GeoDataFrame:
    """Iteratively remove out-of-city dead-end segment chains.

    After boundary splitting, out-of-city segments whose non-virtual endpoint
    is a dead-end (degree=1) cannot reach any in-city block and are removed.
    Iteration continues until no more dead-ends remain, unpeeling chains.
    Segments that bridge two boundary re-entry points are preserved.
    """
    if len(segments_gdf) == 0:
        return segments_gdf

    utm_crs = segments_gdf.estimate_utm_crs()
    segs_utm = segments_gdf.to_crs(utm_crs)
    boundary_utm = unary_union(boundary_gdf.to_crs(utm_crs).geometry)

    midpoints = segs_utm.geometry.interpolate(0.5, normalized=True)
    is_out = ~gpd.GeoSeries(midpoints, crs=utm_crs).within(boundary_utm)

    node_degree: dict[int, int] = defaultdict(int)
    for _, row in segments_gdf.iterrows():
        node_degree[int(row["start_node_id"])] += 1
        node_degree[int(row["end_node_id"])] += 1

    keep = pd.Series(True, index=segments_gdf.index)

    changed = True
    while changed:
        changed = False
        for idx in segments_gdf.index:
            if not keep[idx] or not is_out[idx]:
                continue
            s = int(segments_gdf.loc[idx, "start_node_id"])
            e = int(segments_gdf.loc[idx, "end_node_id"])
            s_dead = s < virtual_id_start and node_degree[s] == 1
            e_dead = e < virtual_id_start and node_degree[e] == 1
            if s_dead or e_dead:
                keep[idx] = False
                node_degree[s] -= 1
                node_degree[e] -= 1
                changed = True

    n_removed = int((~keep).sum())
    if n_removed:
        _logger.info(
            "segment  remove_out_of_city_deadends — removed %d segments", n_removed
        )
    return segments_gdf[keep].reset_index(drop=True)


# ── Trail cluster detection ────────────────────────────────────────────────────

def _matcher_mask(matcher: Matcher, df: pd.DataFrame) -> pd.Series:
    """Vectorised ``any``-of-rows evaluation of a :class:`Matcher` over ``df``."""
    result = pd.Series(False, index=df.index)
    for row in matcher.rows:
        row_mask = pd.Series(True, index=df.index)
        for clause in row.clauses:
            row_mask &= clause.mask(df)
        result |= row_mask
    return result


def _network_path_mask(
    segments_df: gpd.GeoDataFrame, config: BNAConfig
) -> pd.Series | None:
    """Boolean mask of trail-candidate segments from active ``network_path`` entries.

    Returns ``None`` when no ``network_path`` entry is active (the catalog declares
    no trail type) — the caller then produces no trail chains. The default catalog's
    ``trails`` entry matches ``functional_class == "path"``, reproducing the legacy
    hard-coded filter exactly.
    """
    registry = config.destinations
    if registry is None:
        from bikescore.destinations import default_destination_registry
        registry = default_destination_registry()
    entries = [t for t in registry.active() if t.type == "network_path"]
    if not entries:
        return None
    mask = pd.Series(False, index=segments_df.index)
    for entry in entries:
        mask |= _matcher_mask(entry.way_match, segments_df)
    return mask


def detect_trails(
    segments_df: gpd.GeoDataFrame,
    config: BNAConfig,
) -> pd.DataFrame:
    """Detect connected path segment clusters.

    Matches ``features/paths.sql``: groups path segments into connected
    components, then computes path_length and bbox_length for each cluster.

    If segments_df has start_node_id / end_node_id columns, union-find on node
    IDs is used (fast, exact). Otherwise falls back to spatial touching (for
    testing with already-segmented reference data that lacks node columns).

    Args:
        segments_df: GeoDataFrame in the projected CRS. Must have
            'functional_class', 'road_id', and 'geometry' columns.
        config: Pipeline configuration (min_path_length / min_bbox_length not
            applied here — caller filters as needed).

    Returns:
        DataFrame with columns: path_id, road_ids, path_length (int, metres),
        bbox_length (int, metres). All clusters returned; qualifying filter is
        the caller's responsibility.
    """
    empty = pd.DataFrame(columns=["path_id", "road_ids", "path_length", "bbox_length"])

    path_mask = _network_path_mask(segments_df, config)
    if path_mask is None:
        # No active network_path entry in the catalog → no trail chains.
        return empty
    paths = segments_df[path_mask].copy().reset_index(drop=True)
    if len(paths) == 0:
        return empty

    use_nodes = "start_node_id" in paths.columns and "end_node_id" in paths.columns

    if use_nodes:
        comp_members = _cluster_by_nodes(paths)
    else:
        comp_members = _cluster_by_touching(paths)

    return _build_trails_df(comp_members)


def _cluster_by_nodes(paths: gpd.GeoDataFrame) -> dict[int, list[dict]]:
    """Union-find on start/end node IDs → connected components."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        if x not in parent:
            parent[x] = x
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    rows = paths[["start_node_id", "end_node_id", "road_id", "geometry"]].to_dict("records")
    for row in rows:
        union(int(row["start_node_id"]), int(row["end_node_id"]))

    comp_members: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        comp_members[find(int(row["start_node_id"]))].append(row)
    return comp_members


def _cluster_by_touching(paths: gpd.GeoDataFrame) -> dict[int, list[dict]]:
    """Spatial union-find on touching geometries (eps=0 DBSCAN equivalent)."""
    from shapely.strtree import STRtree

    geoms = list(paths.geometry)
    road_ids_col = list(paths["road_id"])
    n = len(geoms)
    parent_list = list(range(n))

    def find(x: int) -> int:
        while parent_list[x] != x:
            parent_list[x] = parent_list[parent_list[x]]
            x = parent_list[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent_list[ra] = rb

    tree = STRtree(geoms)
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            if j != i:
                union(i, int(j))

    comp_members: dict[int, list[dict]] = defaultdict(list)
    for i in range(n):
        comp_members[find(i)].append({"geometry": geoms[i], "road_id": road_ids_col[i]})
    return comp_members


def _build_trails_df(comp_members: dict[int, list[dict]]) -> pd.DataFrame:
    """Compute path_length, bbox_length, road_ids per cluster and return DataFrame."""
    trails: list[dict] = []
    path_id = 1
    for members in comp_members.values():
        geoms = [m["geometry"] for m in members]
        road_ids = [int(m["road_id"]) for m in members]

        combined = unary_union(geoms)
        path_length = round(combined.length)

        b = combined.bounds  # (minx, miny, maxx, maxy) in projected metres
        bbox_length = round(math.sqrt((b[2] - b[0]) ** 2 + (b[3] - b[1]) ** 2))

        trails.append({
            "path_id": path_id,
            "road_ids": road_ids,
            "path_length": path_length,
            "bbox_length": bbox_length,
        })
        path_id += 1

    return pd.DataFrame(trails, columns=["path_id", "road_ids", "path_length", "bbox_length"])


# ── Main entry point ──────────────────────────────────────────────────────────

def segment(
    ways_df: gpd.GeoDataFrame,
    nodes_df: pd.DataFrame,
    config: BNAConfig,
    all_ways_df: gpd.GeoDataFrame | None = None,
    boundary: gpd.GeoDataFrame | None = None,
) -> tuple[SegmentResult, pd.DataFrame]:
    """Split OSM ways at intersections and detect trail clusters.

    Args:
        ways_df: Classified ways GeoDataFrame in EPSG:4326. Must have
            'node_ids' (list[int]) and 'geometry' columns. All classification
            columns (functional_class, bike_infra, one_way, ft/tf_park, etc.)
            are propagated to every split segment.
        nodes_df: Node DataFrame with 'node_id', 'lon', 'lat' columns.
        config: Pipeline configuration. Uses config.output_srid (auto-detected
            from CRS centroid if None).
        all_ways_df: Optional full set of clipped ways (pre-classify) used for
            node_usage in topology splitting. When provided, classified ways are
            split at junctions with footways/service roads (matching osm2pgrouting).
        boundary: City boundary GeoDataFrame (EPSG:4326). When provided,
            segments crossing the boundary are split at the crossing point and
            out-of-city dead-end chains are removed.

    Returns:
        result: SegmentResult with segments GeoDataFrame (output_srid) and
            orphan_osm_ids frozenset. Segments columns: segment_id,
            start_node_id, end_node_id, road_id, geometry, plus all
            classification columns from ways_df. Orphan ways are excluded.
        trails_df: DataFrame of all path clusters. Columns: path_id, road_ids,
            path_length (int metres), bbox_length (int metres). Caller filters
            by config.min_path_length / config.min_bbox_length for scoring.
    """
    _logger.info("segment  input — ways=%d  nodes=%d", len(ways_df), len(nodes_df))

    # ── Determine output CRS ──────────────────────────────────────────────────
    if config.output_srid is not None:
        output_crs = f"EPSG:{config.output_srid}"
    else:
        output_crs = ways_df.geometry.estimate_utm_crs()

    # ── Orphan detection ──────────────────────────────────────────────────────
    node_usage = _compute_node_usage(ways_df)
    orphan_osm_ids = _detect_orphan_osm_ids(ways_df, node_usage)
    _logger.info("segment  orphans=%d osm_ids", len(orphan_osm_ids))

    # ── Topology splitting ────────────────────────────────────────────────────
    segments_df = split_ways(ways_df, nodes_df, all_ways_df=all_ways_df)
    _logger.info("segment  split_ways → %d segments", len(segments_df))

    empty_trails = pd.DataFrame(columns=["path_id", "road_ids", "path_length", "bbox_length"])
    if segments_df.empty:
        return SegmentResult(segments=segments_df, orphan_osm_ids=orphan_osm_ids), empty_trails

    # ── Boundary split ────────────────────────────────────────────────────────
    if boundary is not None:
        virtual_id_start = int(nodes_df["node_id"].max()) + 1
        segments_df, _ = split_at_boundary(segments_df, boundary, virtual_id_start)
        segments_df = remove_out_of_city_deadends(segments_df, boundary, virtual_id_start)
        _logger.info("segment  post-boundary-split → %d segments", len(segments_df))

    # ── Remove orphan segments ────────────────────────────────────────────────
    if orphan_osm_ids and "osm_id" in segments_df.columns:
        n_before_orphan_filter = len(segments_df)
        segments_df = segments_df[
            ~segments_df["osm_id"].isin(orphan_osm_ids)
        ].reset_index(drop=True)
        _logger.info(
            "segment  orphan filter — %d → %d segments (removed %d)",
            n_before_orphan_filter, len(segments_df), n_before_orphan_filter - len(segments_df),
        )

    # ── Reproject to output CRS for distance calculations ─────────────────────
    segments_df = segments_df.to_crs(output_crs)

    # ── Trail cluster detection ───────────────────────────────────────────────
    trails_df = detect_trails(segments_df, config)
    _logger.info("segment  trails — %d path clusters", len(trails_df))

    _logger.info("segment  output — segments=%d  orphans=%d  trails=%d", len(segments_df), len(orphan_osm_ids), len(trails_df))
    return SegmentResult(segments=segments_df, orphan_osm_ids=orphan_osm_ids), trails_df


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

from bikescore.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    attributes_dir = Path(input_paths["attributes"])
    parse_dir = Path(input_paths["parse"])

    ways_df = gpd.read_parquet(attributes_dir / "ways_classified.parquet")
    nodes_df = pd.read_parquet(parse_dir / "nodes.parquet")
    all_ways_df = gpd.read_parquet(parse_dir / "ways_raw.parquet")

    boundary_path = input_paths.get("dataset:boundary")
    boundary = gpd.read_file(boundary_path).to_crs(epsg=4326) if boundary_path else None

    seg_result, trails_df = segment(ways_df, nodes_df, config, all_ways_df=all_ways_df, boundary=boundary)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    seg_result.segments.to_parquet(out / "segments.parquet")
    trails_df.to_parquet(out / "trails.parquet")


SEGMENT = StageSpec(
    name="segment",
    depends_on=("attributes", "parse"),
    dataset_inputs=("boundary",),
    version=STAGE_VERSION,
    run=_run,
)
