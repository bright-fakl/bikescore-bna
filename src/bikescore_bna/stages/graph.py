"""Graph stage: CSR routing graph construction from network tables."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import scipy.sparse as sp

from bikescore_bna.config import BNAConfig

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    import geopandas as gpd


_logger = logging.getLogger("bikescore-bna")

@dataclass
class GraphBundle:
    """CSR routing graphs and block association tables for Dijkstra connectivity.

    Two parallel directed graphs are built: G_high (all roads) and G_low (roads
    with link_stress <= low_stress_threshold). Optional extra graphs at additional
    thresholds live in extra_graphs.

    block_src_verts[i] contains the matrix indices of source verts for block i,
    filtered to boundary_road_ids only (buffer-zone roads are excluded as sources).
    vert_to_blocks[v] lists the block indices associated with matrix vert v.
    """

    n_verts: int
    G_high: sp.csr_matrix
    G_low: sp.csr_matrix
    extra_graphs: dict[int, sp.csr_matrix]
    low_stress_threshold: int
    vert_to_idx: dict[int, int]
    road_to_vert: dict[int, int]
    boundary_road_ids: set[int]
    block_geoids: list[str]
    block_road_sets: list[frozenset[int]]
    block_src_verts: list[np.ndarray]
    vert_to_blocks: list[list[int]]
    block_shapes: list | None = None
    # Shapely geometries (in projected CRS, metres) for each block in block_geoids.
    # Populated from blocks_df.geometry_wkt when that column is present.
    # Used by compute_connectivity to apply the ST_DWithin polygon distance filter
    # that matches connected_census_blocks.sql.
    block_in_boundary: list[bool] | None = None
    # True if the block polygon intersects the city boundary. Matches the SQL
    # ST_Intersects(source.geom, neighborhood_boundary.geom) filter that restricts
    # which blocks are eligible Dijkstra sources in connected_census_blocks.sql.


def _dedup_links(links_df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate directed links, keeping MIN(link_cost) per (source, target) pair.

    Matches GROUP BY source_vert, target_vert in the SQL reference.
    """
    return (
        links_df.groupby(["source_vert", "target_vert"], as_index=False, sort=False)
        .agg(link_cost=("link_cost", "min"))
    )


def _make_csr(
    links: pd.DataFrame,
    n: int,
    vert_to_idx: dict[int, int],
) -> sp.csr_matrix:
    """Build a CSR adjacency matrix from a deduplicated links DataFrame."""
    if links.empty:
        return sp.csr_matrix((n, n), dtype=np.int32)
    row = links["source_vert"].map(vert_to_idx).to_numpy(dtype=np.int32)
    col = links["target_vert"].map(vert_to_idx).to_numpy(dtype=np.int32)
    data = links["link_cost"].to_numpy(dtype=np.int32)
    return sp.csr_matrix((data, (row, col)), shape=(n, n))


def build_graph(
    net_verts: pd.DataFrame,
    net_links: pd.DataFrame,
    blocks_df: pd.DataFrame,
    boundary: object,
    config: BNAConfig,
    segments_df: gpd.GeoDataFrame | None = None,
) -> GraphBundle:
    """Build CSR routing graphs and block association tables from network tables.

    Args:
        net_verts: Vert-to-road mapping. Columns: vert_id (int64), road_id (int64).
        net_links: Directed road links. Columns: source_vert, target_vert (int64),
            link_cost (int64, metres), link_stress (int64).
        blocks_df: Census blocks. Must have geoid20 (str) and road_ids (array of
            int64) columns. geometry_wkt column optional.
        boundary: City boundary GeoDataFrame. Required when segments_df is provided
            to compute boundary_road_ids via spatial intersection.
        config: Pipeline configuration. Uses config.graph.low_stress_threshold
            and config.graph.extra_thresholds.
        segments_df: Road segments GeoDataFrame with geometry in a projected CRS.
            When provided, boundary_road_ids is computed via ST_Intersects(boundary,
            segment.geometry), matching the SQL reference exactly. When None, falls
            back to the block-union proxy (less precise: ~35 extra buffer-zone roads).

    Returns:
        GraphBundle with G_high (all roads), G_low (low-stress roads), and
        supporting lookup tables for block-based Dijkstra.
    """
    _logger.info("graph  input — verts=%d  links=%d  blocks=%d", len(net_verts), len(net_links), len(blocks_df))

    # ── Vert index mappings ──────────────────────────────────────────────────
    verts = net_verts.sort_values("vert_id").reset_index(drop=True)
    n = len(verts)
    vert_ids = verts["vert_id"].to_numpy(dtype=np.int64)
    road_ids_arr = verts["road_id"].to_numpy(dtype=np.int64)

    vert_to_idx: dict[int, int] = {int(vid): i for i, vid in enumerate(vert_ids)}
    road_to_vert: dict[int, int] = {int(rid): i for i, rid in enumerate(road_ids_arr)}

    # ── Build G_high: all links, deduplicated ────────────────────────────────
    dedup_all = _dedup_links(net_links)
    G_high = _make_csr(dedup_all, n, vert_to_idx)

    # ── Build G_low: low-stress links, deduplicated ──────────────────────────
    threshold = config.graph.low_stress_threshold
    low_mask = net_links["link_stress"] <= threshold
    dedup_low = _dedup_links(net_links[low_mask])
    G_low = _make_csr(dedup_low, n, vert_to_idx)

    _logger.info("graph  G_high nnz=%d  G_low nnz=%d  (threshold=%d)", G_high.nnz, G_low.nnz, threshold)

    # ── Extra graphs at additional thresholds ────────────────────────────────
    extra_graphs: dict[int, sp.csr_matrix] = {}
    for t in config.graph.extra_thresholds:
        extra_mask = net_links["link_stress"] <= t
        extra_graphs[t] = _make_csr(_dedup_links(net_links[extra_mask]), n, vert_to_idx)

    # ── Boundary road IDs ────────────────────────────────────────────────────
    # Roads that intersect the city boundary are valid Dijkstra sources.
    # Buffer-zone roads (outside the boundary) are traversable intermediate
    # nodes but are excluded as sources. Matches ST_Intersects(boundary, road.geom)
    # in reachable_roads_high_stress_calc.sql.
    boundary_road_ids: set[int]
    _boundary_geom_proj = None  # projected boundary geometry, reused for block_in_boundary
    if segments_df is not None and boundary is not None:
        # Exact spatial intersection: reproject boundary to segment CRS, then
        # use STRtree to find all segments whose geometry intersects the boundary.
        import geopandas as gpd
        from shapely.strtree import STRtree

        boundary_gdf = boundary if isinstance(boundary, gpd.GeoDataFrame) else gpd.GeoDataFrame(geometry=[boundary])
        boundary_proj = boundary_gdf.to_crs(segments_df.crs)
        _boundary_geom_proj = boundary_proj.geometry.iloc[0]

        tree = STRtree(segments_df.geometry)
        hit_indices = tree.query(_boundary_geom_proj, predicate="intersects")
        boundary_road_ids = {
            int(segments_df.iloc[int(i)]["road_id"]) for i in hit_indices
        }
    elif "road_ids" in blocks_df.columns:
        # Fallback proxy: union of all block road_ids. Less precise than spatial
        # intersection — includes ~35 buffer-zone roads that appear in block road_ids
        # via the 15m block buffer but don't actually intersect the city boundary.
        boundary_road_ids = set()
        for rids in blocks_df["road_ids"]:
            if rids is not None and len(rids) > 0:
                boundary_road_ids.update(int(r) for r in rids)
    else:
        boundary_road_ids = {int(r) for r in road_ids_arr}

    _logger.info("graph  boundary_roads=%d (%s)", len(boundary_road_ids), "spatial" if segments_df is not None and boundary is not None else "block-union fallback")

    # ── Block data structures ────────────────────────────────────────────────
    # Filter to blocks that have at least one associated road.
    has_roads = blocks_df["road_ids"].notna() & (blocks_df["road_ids"].apply(len) > 0)
    valid = blocks_df[has_roads].reset_index(drop=True)

    block_geoids: list[str] = valid["geoid20"].tolist()
    block_road_sets: list[frozenset[int]] = [
        frozenset(int(r) for r in rids) for rids in valid["road_ids"]
    ]

    # Source verts: only roads intersecting the boundary, matching
    # the filter applied in reachable_roads_*_calc.sql before pgRouting.
    block_src_verts: list[np.ndarray] = [
        np.array(
            [road_to_vert[rid] for rid in rset
             if rid in road_to_vert and rid in boundary_road_ids],
            dtype=np.int32,
        )
        for rset in block_road_sets
    ]

    # vert_to_blocks[v_idx] = block indices reachable from vert v.
    vert_to_blocks: list[list[int]] = [[] for _ in range(n)]
    for block_idx, rset in enumerate(block_road_sets):
        for rid in rset:
            if rid in road_to_vert:
                vert_to_blocks[road_to_vert[rid]].append(block_idx)

    # ── Block polygon shapes (for ST_DWithin polygon distance filter) ────────
    # Loaded from geometry_wkt when present. Used by connectivity to match the
    # SQL's ST_DWithin(source.geom, target.geom, max_trip_distance) pre-filter
    # that rejects block pairs whose polygon boundaries are too far apart.
    block_shapes: list | None = None
    if "geometry_wkt" in blocks_df.columns:
        from shapely import wkt as swkt
        block_shapes = [swkt.loads(wkt_str) for wkt_str in valid["geometry_wkt"]]

    # ── Block-in-boundary filter ─────────────────────────────────────────────
    # Matches ST_Intersects(source.geom, neighborhood_boundary.geom) from
    # connected_census_blocks.sql: only polygon-intersecting blocks are sources.
    block_in_boundary: list[bool] | None = None
    if block_shapes is not None and _boundary_geom_proj is not None:
        block_in_boundary = [_boundary_geom_proj.intersects(s) for s in block_shapes]

    _logger.info("graph  valid_blocks=%d  n_verts=%d", len(block_geoids), n)
    return GraphBundle(
        n_verts=n,
        G_high=G_high,
        G_low=G_low,
        extra_graphs=extra_graphs,
        low_stress_threshold=threshold,
        vert_to_idx=vert_to_idx,
        road_to_vert=road_to_vert,
        boundary_road_ids=boundary_road_ids,
        block_geoids=block_geoids,
        block_road_sets=block_road_sets,
        block_src_verts=block_src_verts,
        vert_to_blocks=vert_to_blocks,
        block_shapes=block_shapes,
        block_in_boundary=block_in_boundary,
    )


def segments_to_network(
    segments_df: gpd.GeoDataFrame,
    config: BNAConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert stressed road segments to net_verts and net_links tables.

    Implements the build_network.sql logic: one vertex per segment, directed
    links between pairs of segments that share an intersection node.

    Link stress is aggregated from the source-segment, intersection, and
    target-segment stresses per ``config.graph.link_stress_model`` (default
    "max" reproduces the BNA reference).
    Link cost   = round((source_length + target_length) / 2) in metres.

    Args:
        segments_df: GeoDataFrame with road_id, start_node_id, end_node_id,
            ft/tf_seg_stress, ft/tf_int_stress, one_way, and a projected geometry.
        config: Pipeline configuration. Uses config.graph.link_stress_model and,
            for the "sum" model, config.stress.n_levels as the cap.

    Returns:
        net_verts: DataFrame with vert_id (int64), road_id (int64).
        net_links: DataFrame with source_vert, target_vert, link_cost, link_stress
            (all int64). Does NOT deduplicate — build_graph handles dedup.
    """
    n = len(segments_df)
    _logger.info("graph  segments_to_network — segments=%d", n)
    road_ids = segments_df["road_id"].values.astype(np.int64)

    net_verts = pd.DataFrame({
        "vert_id": np.arange(1, n + 1, dtype=np.int64),
        "road_id": road_ids,
    })
    vert_ids = net_verts["vert_id"].values  # 0-based index = seg row

    start_nodes = segments_df["start_node_id"].values.astype(np.int64)
    end_nodes = segments_df["end_node_id"].values.astype(np.int64)
    one_way_arr = segments_df["one_way"].values  # object array: None/'ft'/'tf'
    ft_seg = segments_df["ft_seg_stress"].values.astype(float)
    tf_seg = segments_df["tf_seg_stress"].values.astype(float)
    ft_int = segments_df["ft_int_stress"].values.astype(float)
    tf_int = segments_df["tf_int_stress"].values.astype(float)
    lengths = segments_df.geometry.length.values  # metres (projected CRS)

    seg_indices = np.arange(n)

    # Build endpoint table: each segment contributes two endpoints
    ep_start = pd.DataFrame({
        "node_id": start_nodes, "seg_idx": seg_indices, "at_end": False,
    })
    ep_end = pd.DataFrame({
        "node_id": end_nodes, "seg_idx": seg_indices, "at_end": True,
    })
    endpoints = pd.concat([ep_start, ep_end], ignore_index=True)

    # Self-join all pairs of segments sharing a node
    pairs = endpoints.merge(endpoints, on="node_id", suffixes=("_s", "_t"))
    pairs = pairs[pairs["seg_idx_s"] != pairs["seg_idx_t"]].reset_index(drop=True)

    si = pairs["seg_idx_s"].values
    ti = pairs["seg_idx_t"].values
    at_end_s = pairs["at_end_s"].values
    at_end_t = pairs["at_end_t"].values

    ow_s = one_way_arr[si]
    ow_t = one_way_arr[ti]

    # Source can exit at this node:
    # two-way: always; ft: exit at end_node; tf: exit at start_node
    is_none_s = np.array([v is None or (isinstance(v, float) and np.isnan(v)) for v in ow_s])
    can_exit = (
        is_none_s
        | (np.array([v == "ft" for v in ow_s]) & at_end_s)
        | (np.array([v == "tf" for v in ow_s]) & ~at_end_s)
    )

    # Target can be entered at this node:
    # two-way: always; ft: enter at start_node; tf: enter at end_node
    is_none_t = np.array([v is None or (isinstance(v, float) and np.isnan(v)) for v in ow_t])
    can_enter = (
        is_none_t
        | (np.array([v == "ft" for v in ow_t]) & ~at_end_t)
        | (np.array([v == "tf" for v in ow_t]) & at_end_t)
    )

    mask = can_exit & can_enter
    si = si[mask]
    ti = ti[mask]
    at_end_s = at_end_s[mask]
    at_end_t = at_end_t[mask]

    # Stresses
    source_stress = np.where(at_end_s, ft_seg[si], tf_seg[si])
    int_stress    = np.where(at_end_s, ft_int[si], tf_int[si])
    # target: entering road2 from this node; if at end → travels backward (tf)
    target_stress = np.where(at_end_t, tf_seg[ti], ft_seg[ti])

    # Drop rows where any stress is NaN (one-way blocked direction → NULL stress)
    valid = ~(np.isnan(source_stress) | np.isnan(int_stress) | np.isnan(target_stress))
    si = si[valid]
    ti = ti[valid]
    source_stress = source_stress[valid]
    int_stress = int_stress[valid]
    target_stress = target_stress[valid]

    model = config.graph.link_stress_model
    if model == "max":
        link_stress = np.maximum(np.maximum(source_stress, int_stress), target_stress)
    elif model == "segment_only":
        link_stress = np.maximum(source_stress, target_stress)
    elif model == "sum":
        n_levels = config.stress.n_levels
        link_stress = np.minimum(source_stress + int_stress + target_stress, n_levels)
    else:
        raise ValueError(f"unknown link_stress_model: {model!r}")
    link_stress = link_stress.astype(np.int64)
    # Match SQL: round each length to integer first, then integer-divide.
    # SQL stores road lengths as INTEGER (rounded), then link_cost = (int+int)/2
    # using integer division (truncation). Python round((a+b)/2) on floats rounds
    # differently for half-integer sums — replicate the SQL by rounding first.
    link_cost = (np.round(lengths[si]).astype(np.int64) + np.round(lengths[ti]).astype(np.int64)) // 2

    net_links = pd.DataFrame({
        "source_vert": vert_ids[si].astype(np.int64),
        "target_vert": vert_ids[ti].astype(np.int64),
        "link_cost": link_cost,
        "link_stress": link_stress,
    })

    _logger.info("graph  segments_to_network — verts=%d  links=%d", len(net_verts), len(net_links))
    return net_verts, net_links


def filter_census_blocks(
    blocks_gdf: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    block_boundary_overlap: float = 0.50,
    exclude_water_blocks: bool = True,
) -> gpd.GeoDataFrame:
    """Remove census blocks that are mostly outside the city boundary.

    Mirrors brokenspoke-analyzer's delete_block_outside_buffer() and
    delete_water_blocks():
    - Drops water-only blocks (aland20 == 0) when ``exclude_water_blocks``.
    - Drops blocks where ST_AREA(ST_INTERSECTION(block, boundary)) /
      ST_AREA(block) < ``block_boundary_overlap`` (less than this fraction of the
      block lies inside the boundary).

    Args:
        blocks_gdf: Census blocks GeoDataFrame (any CRS).
        boundary: City boundary GeoDataFrame (any CRS).
        block_boundary_overlap: Minimum in-boundary area fraction to keep a block.
        exclude_water_blocks: Drop blocks with zero land area (aland20 == 0).

    Returns:
        Filtered copy of blocks_gdf.
    """
    from shapely.ops import unary_union

    result = blocks_gdf.copy()

    if exclude_water_blocks and "aland20" in result.columns:
        result = result[result["aland20"] > 0].reset_index(drop=True)

    blocks_ea = result.to_crs(epsg=6933)
    boundary_ea = boundary.to_crs(epsg=6933)
    boundary_geom_ea = unary_union(boundary_ea.geometry)
    block_areas = blocks_ea.geometry.area
    intersect_areas = blocks_ea.geometry.intersection(boundary_geom_ea).area
    keep = (intersect_areas / block_areas) >= block_boundary_overlap
    return result[keep].reset_index(drop=True)


def compute_block_road_ids(
    blocks_gdf: gpd.GeoDataFrame,
    segments_df: gpd.GeoDataFrame,
    config: BNAConfig,
) -> gpd.GeoDataFrame:
    """Add road_ids column to blocks: road segments that overlap each block buffer.

    Implements census_blocks.sql: buffers each block by block_road_buffer metres,
    then keeps segments that are fully contained in the buffer OR whose intersection
    length exceeds block_road_min_length.

    Args:
        blocks_gdf: Census blocks GeoDataFrame (any CRS; reprojected internally).
        segments_df: Road segments GeoDataFrame in a projected CRS (metres).
        config: BNAConfig providing block_road_buffer and block_road_min_length.

    Returns:
        Copy of blocks_gdf with road_ids column (list[int] per block) and
        geometry_wkt column (WKT of block geometry in segments CRS).
    """
    from shapely.strtree import STRtree

    seg_crs = segments_df.crs
    blocks_proj = blocks_gdf.to_crs(seg_crs)
    buffer_m = float(config.block_road_buffer)
    min_length_m = float(config.block_road_min_length)

    road_ids_list = segments_df["road_id"].values.astype(np.int64)
    seg_geoms = list(segments_df.geometry)
    tree = STRtree(seg_geoms)

    road_ids_col: list[list[int]] = []
    wkt_col: list[str] = []

    for geom in blocks_proj.geometry:
        if geom is None or geom.is_empty:
            road_ids_col.append([])
            wkt_col.append("")
            continue

        try:
            buf = geom.buffer(buffer_m)
        except Exception:
            # Invalid geometry — try make_valid first
            from shapely.validation import make_valid
            try:
                geom = make_valid(geom)
                buf = geom.buffer(buffer_m)
            except Exception:
                road_ids_col.append([])
                wkt_col.append("")
                continue

        candidate_idxs = tree.query(buf, predicate="intersects")
        rids = []
        for j in candidate_idxs:
            seg = seg_geoms[j]
            # Matches SQL: ST_Contains(buffer, road) OR ST_Length(intersection) > min.
            # Short segments fully inside the buffer pass even if length < min_length_m.
            if buf.contains(seg):
                rids.append(int(road_ids_list[j]))
                continue
            intersection = buf.intersection(seg)
            if intersection.length > min_length_m:
                rids.append(int(road_ids_list[j]))
        road_ids_col.append(rids)
        wkt_col.append(geom.wkt)

    result = blocks_gdf.copy()
    result["road_ids"] = road_ids_col
    result["geometry_wkt"] = wkt_col
    return result


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

import pickle  # noqa: E402
from pathlib import Path  # noqa: E402

from bikescore_bna.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd

    stress_dir = Path(input_paths["stress"])
    census_dir = Path(input_paths["census"])

    segments_df = gpd.read_parquet(stress_dir / "stress.parquet")
    blocks_gdf = gpd.read_parquet(census_dir / "census_blocks.parquet")

    if "road_ids" not in blocks_gdf.columns:
        blocks_gdf = compute_block_road_ids(blocks_gdf, segments_df, config)

    net_verts, net_links = segments_to_network(segments_df, config)
    graph_bundle = build_graph(net_verts, net_links, blocks_gdf, None, config, segments_df)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "graph_bundle.pkl", "wb") as f:
        pickle.dump(graph_bundle, f)
    net_verts.to_parquet(out / "nodes.parquet")
    net_links.to_parquet(out / "graph.parquet")
    blocks_gdf.to_parquet(out / "blocks_with_roads.parquet")


GRAPH = StageSpec(
    name="graph",
    depends_on=("stress", "census"),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
