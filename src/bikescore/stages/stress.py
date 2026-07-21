"""Stress stage: compute LTS (Level of Traffic Stress) for each road segment.

Produces four columns:
- ft_seg_stress / tf_seg_stress: segment stress in each travel direction
- ft_int_stress / tf_int_stress: intersection crossing stress at each end

Architecture
------------
Segment stress (two steps):
  1. _apply_segment_rules(): sets adj_fc = functional_class, fires config.stress.segment_rules
  2. functional_class is already adjusted (residential/unclassified → tertiary) by the
     classify stage; speed/lane/width defaults are pre-filled by the attributes stage

Intersection stress (two steps):
  1. _build_intersection_context(): fixed cross-row JOIN; produces boolean context
     columns (has_high_cross_ft/tf etc.) + node attribute columns
  2. config.stress.intersection_rules.apply(enriched_df): RuleSet fires on context

Mirrors the 12 SQL scripts in brokenspoke_analyzer/scripts/sql/stress/.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore.config import BNAConfig
    from bikescore.decision import Decision


# ── Functional class groupings ────────────────────────────────────────────────

_logger = logging.getLogger("bikescore")

_MOTORWAY_FCS = frozenset({"motorway", "trunk", "motorway_link", "trunk_link"})
_PRIMARY_FCS = frozenset({"primary", "primary_link"})
_SECONDARY_FCS = frozenset({"secondary", "secondary_link"})
_TERTIARY_FCS = frozenset({"tertiary", "tertiary_link"})
_LINK_FCS = frozenset({"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"})
_LESSER_FCS = frozenset({"residential", "unclassified", "living_street", "track", "path"})

# Eligible crossing FCs (SQL EXISTS CASE: never _link variants)
_CROSSING_ALL = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})
_CROSSING_NO_TERT = frozenset({"motorway", "trunk", "primary", "secondary"})






def _apply_segment_rules(
    df: pd.DataFrame,
    adj_fc: pd.Series,
    rules: Decision,
    variables: dict | None = None,
) -> pd.DataFrame:
    """Apply segment stress rules. Defaults are pre-filled by the attributes stage."""
    df = df.copy()
    df["adj_fc"] = adj_fc
    return rules.apply(df, variables=variables)


# ── One-way reset (mirrors stress_one_way_reset.sql) ─────────────────────────

def _apply_one_way_reset(df: pd.DataFrame) -> pd.DataFrame:
    """Null out the blocked direction for one-way roads.

    UPDATE SET ft_seg_stress = NULL WHERE one_way = 'tf';
    UPDATE SET tf_seg_stress = NULL WHERE one_way = 'ft';
    """
    if "one_way" not in df.columns:
        return df
    df = df.copy()
    one_way = df["one_way"]
    df.loc[one_way == "tf", "ft_seg_stress"] = np.nan
    df.loc[one_way == "ft", "tf_seg_stress"] = np.nan
    return df


def _apply_intersection_rules(
    df: pd.DataFrame,
    adj_fc: pd.Series,
    nodes_df: pd.DataFrame,
    rules: Decision,
    node_key: str = "osm_id",
    crossing_speed_defaults: dict[str, int] | None = None,
    variables: dict | None = None,
) -> pd.DataFrame:
    """Evaluate the intersection-stress decision via the engine.

    The decision's ``int_stress`` pass defaults every road to stress 1 and fires 3
    only where its (provider-supplied) context columns match; the ``after`` link
    pass resets ``_link`` roads to 1 (stress_link_ints.sql). The
    ``intersection_context`` provider runs only because the rules reference its
    fields, replacing the formerly hardcoded cross-row join.
    """
    from bikescore.decision import run_decision
    from bikescore.rules.providers import INTERSECTION_CATALOG

    if node_key in nodes_df.columns:
        node_attrs = nodes_df.set_index(node_key)[["signalized", "stops", "rrfb", "island"]]
    else:
        node_attrs = nodes_df[["signalized", "stops", "rrfb", "island"]]

    df = df.copy()
    df["adj_fc"] = adj_fc
    return run_decision(
        rules, df, INTERSECTION_CATALOG,
        extras={
            "node_attrs": node_attrs,
            "adj_fc": adj_fc,
            "node_key": node_key,
            "crossing_speed_defaults": crossing_speed_defaults,
        },
        variables=variables,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def _propagate_node_attributes(
    nodes_df: pd.DataFrame,
    segments_df: pd.DataFrame,
    search_dist_m: float = 25.0,
    node_key: str = "osm_id",
) -> pd.DataFrame:
    """Propagate signal/stop/rrfb/island from directly-tagged nodes to nearby intersections.

    Mirrors the second pass of signalized.sql, stops.sql, rrfb.sql, island.sql:
    any intersection with legs > 2 inherits an attribute if any directly-tagged
    source node of that attribute type is within search_dist_m metres.

    Uses a degree approximation for the spatial search (25m / 111320 ≈ 0.000225°),
    adequate for any latitude where BNA is used.

    Args:
        nodes_df: Node table with node_key, lon, lat, signalized, stop/stops, rrfb, island.
        segments_df: Segments with start_node_id and end_node_id (for legs count).
        search_dist_m: Search radius in metres. Default 25 matches SQL reference.
        node_key: Column identifying each node (default 'osm_id'; use 'node_id' for parse output).

    Returns:
        Updated copy of nodes_df with propagated attributes.
    """
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    nodes = nodes_df.copy()

    # Compute legs per node (number of segment endpoints at this node)
    id_col = node_key if node_key in nodes.columns else "node_id"
    if id_col not in nodes.columns:
        return nodes  # can't compute legs without node IDs

    seg_start = segments_df["start_node_id"] if "start_node_id" in segments_df.columns else pd.Series(dtype=int)
    seg_end = segments_df["end_node_id"] if "end_node_id" in segments_df.columns else pd.Series(dtype=int)
    endpoint_counts = pd.concat([seg_start, seg_end]).value_counts()
    nodes["_legs"] = nodes[id_col].map(endpoint_counts).fillna(0).astype(int)
    intersection_mask = nodes["_legs"] > 2

    # Degree approximation for 25m search radius
    deg_radius = search_dist_m / 111320.0

    # Detect column name variant (parse produces 'stop', stress expects 'stops')
    attr_cols = []
    for attr in ("signalized", "stops", "stop", "rrfb", "island"):
        if attr in nodes.columns:
            attr_cols.append(attr)

    for col in attr_cols:
        source_mask = nodes[col].eq(True)
        source_nodes = nodes[source_mask]
        if source_nodes.empty:
            continue

        # Build STRtree from source node points (vectorised — no iterrows)
        src_pts = [Point(lon, lat)
                   for lon, lat in zip(source_nodes["lon"], source_nodes["lat"])]
        src_tree = STRtree(src_pts)

        # Bulk-query: buffer each candidate intersection and find source nodes within range.
        # STRtree.query(geometry_array) returns (candidate_positions, source_positions).
        candidate_intersections = nodes[intersection_mask & ~source_mask]
        if candidate_intersections.empty:
            continue
        cand_buffered = [Point(lon, lat).buffer(deg_radius)
                         for lon, lat in zip(candidate_intersections["lon"],
                                             candidate_intersections["lat"])]
        result = src_tree.query(cand_buffered, predicate="intersects")
        # result[0] = positions into cand_buffered; result[1] = positions into src_pts
        if len(result[0]):
            hit_positions = set(result[0])
            hit_indices = [candidate_intersections.index[pos] for pos in hit_positions]
            nodes.loc[hit_indices, col] = True

    nodes = nodes.drop(columns=["_legs"])
    return nodes


def compute_stress(
    segments_df: gpd.GeoDataFrame | pd.DataFrame,
    nodes_df: pd.DataFrame,
    config: BNAConfig,
    node_key: str = "osm_id",
    variables: dict | None = None,
) -> gpd.GeoDataFrame:
    """Compute LTS stress values for each road segment.

    Wires config.stress.segment_rules and config.stress.intersection_rules into
    the computation — custom RuleSets in config will affect the output.

    Args:
        segments_df: Road segments. Required columns: road_id, functional_class,
            speed_limit, ft_lanes, tf_lanes, ft_bike_infra, tf_bike_infra, one_way, name.
            Required for intersection stress: start_node_id, end_node_id.
            All speed/lane/width defaults must be pre-filled by the attributes stage.
        nodes_df: Intersection nodes.
            Required columns: node_key (default 'osm_id'), signalized, stops, rrfb, island.
        config: BNAConfig; stress sub-config provides segment_rules and intersection_rules.
        node_key: Column name in nodes_df identifying each node.

    Returns:
        segments_df with ft_seg_stress, tf_seg_stress, ft_int_stress, tf_int_stress added.
    """
    df = pd.DataFrame(segments_df) if not isinstance(segments_df, pd.DataFrame) else segments_df.copy()
    _logger.info("stress  input — segments=%d", len(df))

    # Normalize node attribute column naming: parse() produces 'stop',
    # this stage's contract (and SQL reference) uses 'stops'.
    if "stop" in nodes_df.columns and "stops" not in nodes_df.columns:
        nodes_df = nodes_df.rename(columns={"stop": "stops"})

    # 0. Propagate signal/stop/rrfb/island attributes from nearby tagged nodes to
    #    multi-leg intersections (legs > 2). Mirrors the second pass in signalized.sql,
    #    stops.sql, rrfb.sql, island.sql. Runs only when nodes have lon/lat geometry.
    if "lon" in nodes_df.columns and "lat" in nodes_df.columns and "start_node_id" in df.columns:
        nodes_df = _propagate_node_attributes(nodes_df, df, node_key=node_key)

    # 1. Use functional_class as-is — class_adjustments ran in classify stage.
    adj_fc = df["functional_class"].copy() if "functional_class" in df.columns else pd.Series(dtype=str, index=df.index)

    # 2. Apply segment rules
    df = _apply_segment_rules(df, adj_fc, config.stress.segment_rules, variables=variables)
    _ft_seg = df["ft_seg_stress"].value_counts().sort_index() if "ft_seg_stress" in df.columns else {}
    _tf_seg = df["tf_seg_stress"].value_counts().sort_index() if "tf_seg_stress" in df.columns else {}
    _logger.info(
        "stress  ft_seg_stress — %s",
        "  ".join(f"{k}={v}" for k, v in _ft_seg.items()) or "none",
    )
    _logger.info(
        "stress  tf_seg_stress — %s",
        "  ".join(f"{k}={v}" for k, v in _tf_seg.items()) or "none",
    )

    # 3. One-way reset (NULL out blocked direction)
    df = _apply_one_way_reset(df)

    # 4. Apply intersection rules (builds context columns, then calls RuleSet.apply())
    df = _apply_intersection_rules(
        df, adj_fc, nodes_df, config.stress.intersection_rules, node_key,
        crossing_speed_defaults=config.stress.crossing_speed_defaults,
        variables=variables,
    )
    _ft_int = df["ft_int_stress"].value_counts().sort_index() if "ft_int_stress" in df.columns else {}
    _tf_int = df["tf_int_stress"].value_counts().sort_index() if "tf_int_stress" in df.columns else {}
    _logger.info(
        "stress  ft_int_stress — %s",
        "  ".join(f"{k}={v}" for k, v in _ft_int.items()) or "none",
    )
    _logger.info(
        "stress  tf_int_stress — %s",
        "  ".join(f"{k}={v}" for k, v in _tf_int.items()) or "none",
    )

    # Restore geometry if original was a GeoDataFrame
    try:
        import geopandas as gpd
        if isinstance(segments_df, gpd.GeoDataFrame):
            return gpd.GeoDataFrame(df, geometry=segments_df.geometry, crs=segments_df.crs)
    except ImportError:
        pass

    return df


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

from bikescore.stage import StageSpec, build_rule_variables  # noqa: E402

# StressStage declared no rule variables in bna-core (it inherited the empty
# BaseStage._RULE_VARIABLES); its ``variables`` are just ``config.variables``.
_RULE_VARIABLES: dict[str, str] = {}


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd
    import pandas as pd

    segment_dir = Path(input_paths["segment"])
    parse_dir = Path(input_paths["parse"])

    segments_df = gpd.read_parquet(segment_dir / "segments.parquet")
    nodes_df = pd.read_parquet(parse_dir / "nodes.parquet")

    variables = {**config.variables, **build_rule_variables(_RULE_VARIABLES, config)}
    result = compute_stress(segments_df, nodes_df, config, node_key="node_id", variables=variables)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.to_parquet(out / "stress.parquet")


STRESS = StageSpec(
    name="stress",
    depends_on=("segment", "parse"),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
