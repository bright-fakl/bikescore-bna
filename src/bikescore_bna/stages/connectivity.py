"""Connectivity stage: block-to-block reachability via scipy Dijkstra."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bikescore_bna.stages.graph import GraphBundle

from bikescore_bna.config import BNAConfig

STAGE_VERSION: str = "1.0.0"

_logger = logging.getLogger("bikescore-bna")


def _worker(
    G_high: object,
    G_low: object,
    extra_graphs: dict[int, object],
    block_chunk: list[tuple],
    block_geoids: list[str],
    vert_to_blocks: list[list[int]],
    block_road_sets: list[frozenset[int]],
    max_trip_distance: int,
    extra_thresholds: list[int],
    low_stress_ratio: float,
    block_shapes: list | None = None,
) -> list[tuple]:
    """Run Dijkstra for a chunk of source blocks; return list of result rows.

    Each row is a tuple:
        (source_blockid20, target_blockid20, low_stress, low_stress_cost,
         high_stress, high_stress_cost [, extra_cost_N, ...])

    Costs are rounded integers; low_stress_cost is None when no low-stress
    path exists within max_trip_distance.
    """
    import scipy.sparse.csgraph as csg

    results: list[tuple] = []

    for block_idx, geoid20, src_verts, src_roads in block_chunk:
        if len(src_verts) == 0:
            continue

        d_high = csg.dijkstra(G_high, indices=src_verts, directed=True, limit=max_trip_distance)
        d_low = csg.dijkstra(G_low, indices=src_verts, directed=True, limit=max_trip_distance)

        d_extras: dict[int, object] = {}
        for t in extra_thresholds:
            d_extras[t] = csg.dijkstra(
                extra_graphs[t], indices=src_verts, directed=True, limit=max_trip_distance
            )

        # Minimum cost across all source verts for each target vert
        min_high = d_high.min(axis=0)  # shape (n_verts,)
        min_low = d_low.min(axis=0)
        min_extras = {t: d_extras[t].min(axis=0) for t in extra_thresholds}

        del d_high, d_low, d_extras

        # Accumulate min costs per target block
        target_hs: dict[int, float] = {}
        target_ls: dict[int, float] = {}
        target_extra: dict[int, dict[int, float]] = {t: {} for t in extra_thresholds}

        reachable = np.where(min_high <= max_trip_distance)[0]
        for v_idx in reachable.tolist():
            hs = float(min_high[v_idx])
            ls = float(min_low[v_idx])
            for tgt_idx in vert_to_blocks[v_idx]:
                if tgt_idx == block_idx:
                    continue
                if tgt_idx not in target_hs or hs < target_hs[tgt_idx]:
                    target_hs[tgt_idx] = hs
                if ls <= max_trip_distance:
                    if tgt_idx not in target_ls or ls < target_ls[tgt_idx]:
                        target_ls[tgt_idx] = ls
                for t in extra_thresholds:
                    e = float(min_extras[t][v_idx])
                    if e <= max_trip_distance:
                        td = target_extra[t]
                        if tgt_idx not in td or e < td[tgt_idx]:
                            td[tgt_idx] = e

        # Build result rows
        for tgt_idx, hs_cost in target_hs.items():
            # Polygon distance filter: matches SQL ST_DWithin(source.geom, target.geom,
            # max_trip_distance) from connected_census_blocks.sql. Rejects pairs where
            # polygon boundaries are >max_trip_distance apart even if the network path
            # is within the limit (e.g. a roundabout detour fits within 2680m of road
            # but the blocks are farther apart geographically).
            if block_shapes is not None:
                if block_shapes[block_idx].distance(block_shapes[tgt_idx]) > max_trip_distance:
                    continue

            ls_cost = target_ls.get(tgt_idx)
            tgt_roads = block_road_sets[tgt_idx]

            # low_stress flag: adjacent blocks always low-stress; otherwise ratio test.
            # Matches connected_census_blocks.sql: road_ids && road_ids OR ratio <= 1.25.
            # COALESCE(high_stress_cost, 0) = 0 → True handles the hs_cost == 0 case.
            if src_roads & tgt_roads:
                low_stress = True
            elif ls_cost is not None:
                low_stress = hs_cost == 0 or ls_cost / hs_cost <= low_stress_ratio
            else:
                low_stress = False

            row: list = [
                geoid20,
                block_geoids[tgt_idx],
                low_stress,
                round(ls_cost) if ls_cost is not None else None,
                True,
                round(hs_cost),
            ]
            for t in extra_thresholds:
                e_cost = target_extra[t].get(tgt_idx)
                row.append(round(e_cost) if e_cost is not None else None)

            results.append(tuple(row))

    return results


def compute_connectivity(
    graph: GraphBundle,
    config: BNAConfig,
) -> pd.DataFrame:
    """Compute block-to-block connectivity using multi-source Dijkstra.

    For each source block, runs Dijkstra simultaneously from all of the block's
    road verts on both the high-stress and low-stress routing graphs.  A target
    block is reachable if any of its road verts are within max_trip_distance.

    A pair is flagged low_stress=True if:
    - The source and target blocks share any road_id (adjacent blocks), OR
    - The low-stress cost divided by the high-stress cost is <= low_stress_ratio.

    Self-pairs (block, block, True, 0, True, 0) are inserted for all blocks
    with non-empty block_src_verts.

    Args:
        graph: GraphBundle from the graph stage (Phase 1).
        config: Pipeline configuration.

    Returns:
        DataFrame with columns: source_blockid20, target_blockid20, low_stress,
        low_stress_cost, high_stress, high_stress_cost (plus low_stress_cost_N
        for each threshold in config.graph.extra_thresholds).
    """
    n_workers = config.connectivity.n_workers or os.cpu_count() or 1
    max_trip_distance = config.max_trip_distance
    extra_thresholds = sorted(graph.extra_graphs.keys())

    bib = graph.block_in_boundary  # None = no polygon filter (fallback mode)
    block_list = [
        (i, graph.block_geoids[i], graph.block_src_verts[i], graph.block_road_sets[i])
        for i in range(len(graph.block_geoids))
        if len(graph.block_src_verts[i]) > 0
        and (bib is None or bib[i])
    ]
    _logger.info(
        "connectivity  input — blocks=%d  blocks_with_src_verts=%d  workers=%d",
        len(graph.block_geoids), len(block_list), n_workers,
    )

    # Divide into fine-grained chunks for better load balancing across workers.
    n_chunks = n_workers * config.connectivity.batches_per_worker
    chunk_size = max(1, (len(block_list) + n_chunks - 1) // n_chunks)
    chunks = [block_list[i:i + chunk_size] for i in range(0, len(block_list), chunk_size)]

    all_rows: list[tuple] = []

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(
                _worker,
                graph.G_high, graph.G_low, graph.extra_graphs,
                chunk,
                graph.block_geoids, graph.vert_to_blocks, graph.block_road_sets,
                max_trip_distance, extra_thresholds,
                config.connectivity.low_stress_ratio, graph.block_shapes,
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            all_rows.extend(future.result())

    _logger.info("connectivity  dijkstra done — pairs=%d", len(all_rows))

    # Self-pairs: (block, block, True, 0, True, 0) for all blocks with src verts.
    if config.connectivity.include_self_pairs:
        for i, geoid20 in enumerate(graph.block_geoids):
            if len(graph.block_src_verts[i]) > 0 and (bib is None or bib[i]):
                row: list = [geoid20, geoid20, True, 0.0, True, 0]
                for _t in extra_thresholds:
                    row.append(0.0)
                all_rows.append(tuple(row))

    cols = [
        "source_blockid20", "target_blockid20",
        "low_stress", "low_stress_cost",
        "high_stress", "high_stress_cost",
    ]
    for t in extra_thresholds:
        cols.append(f"low_stress_cost_{t}")

    df = pd.DataFrame(all_rows, columns=cols)

    # Cast to match reference schema: high_stress_cost → int64, low_stress_cost → float64.
    df["high_stress_cost"] = df["high_stress_cost"].astype(np.int64)
    # low_stress_cost stays float64 (nullable; NaN where no low-stress path exists).

    _n_ls = int(df["low_stress"].sum())
    _logger.info(
        "connectivity  output — pairs=%d  low_stress=%d  high_stress_only=%d",
        len(df), _n_ls, len(df) - _n_ls,
    )
    return df


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

import pickle  # noqa: E402
from pathlib import Path  # noqa: E402

from bikescore_bna.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    graph_dir = Path(input_paths["graph"])
    with open(graph_dir / "graph_bundle.pkl", "rb") as f:
        graph_bundle = pickle.load(f)

    result = compute_connectivity(graph_bundle, config)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.to_parquet(out / "connectivity.parquet")

    csv_df = result.copy()
    for col in ("low_stress", "high_stress"):
        if col in csv_df.columns:
            csv_df[col] = csv_df[col].map({True: "t", False: "f"})
    csv_df.to_csv(out / "connectivity.csv", index=False)


CONNECTIVITY = StageSpec(
    name="connectivity",
    depends_on=("graph",),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
