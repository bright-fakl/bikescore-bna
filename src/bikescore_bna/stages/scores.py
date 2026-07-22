"""Scores stage: per-block access scoring for population, employment, destinations, trails, and overall."""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bikescore_bna.destinations import DestinationRegistry
    from bikescore_bna.stages.graph import GraphBundle

from bikescore_bna.config import BNAConfig, validate_scoring_categories

STAGE_VERSION: str = "1.0.0"

_logger = logging.getLogger("bikescore-bna")


def _piecewise_score(
    h: np.ndarray,
    lo: np.ndarray,
    max_score: float,
    step1: float, score1: float,
    step2: float, score2: float,
    step3: float, score3: float,
) -> np.ndarray:
    """Vectorised piecewise linear ratio formula matching access_population.sql."""
    ratio = np.where((np.isnan(h)) | (h == 0), np.nan, lo / h)
    return np.select(
        [
            np.isnan(ratio),
            ratio == 0,
            ratio >= 1,
            step1 == 0,
            ratio > step3,
            ratio > step2,
            ratio > step1,
        ],
        [
            np.nan,
            0.0,
            float(max_score),
            max_score * ratio,
            score3 + (max_score - score3) * (ratio - step3) / (1 - step3),
            score2 + (score3 - score2) * (ratio - step2) / (step3 - step2),
            score1 + (score2 - score1) * (ratio - step1) / (step2 - step1),
        ],
        default=score1 * (ratio / step1),
    )


def _destination_score_vec(
    low: np.ndarray,
    high: np.ndarray,
    first: float,
    second: float,
    third: float,
    max_score: float,
) -> np.ndarray:
    """Vectorised piecewise destination scoring matching access_*.sql CASE formula."""
    h = high.astype(float)
    lo = low.astype(float)

    return np.where(
        (h == 0) | np.isnan(h), np.nan,
        np.where(lo == 0, 0.0,
        np.where(h == lo, float(max_score),
        np.where(first == 0, lo / h,
        np.where(second == 0,
            first + (max_score - first) * (lo - 1) / np.maximum(h - 1, 1e-9),
        np.where(third == 0,
            np.where(lo == 1, first,
            np.where(lo == 2, first + second,
                first + second
                + (max_score - first - second) * (lo - 2) / np.maximum(h - 2, 1e-9)
            )),
            np.where(lo == 1, first,
            np.where(lo == 2, first + second,
            np.where(lo == 3, first + second + third,
                first + second + third
                + (max_score - first - second - third)
                * (lo - 3) / np.maximum(h - 3, 1e-9)
            )))
        ))))))


def _compute_destination_access(
    ccb: pd.DataFrame,
    dest_df: pd.DataFrame | None,
    result: pd.DataFrame,
    dest_name: str,
    first: float,
    second: float,
    third: float,
    max_score: float,
) -> None:
    """Compute high/low stress counts and score for one destination type (mutates result)."""
    low_col   = f"{dest_name}_low_stress"
    high_col  = f"{dest_name}_high_stress"
    score_col = f"{dest_name}_score"

    if dest_df is None or dest_df.empty:
        result[high_col]  = 0
        result[low_col]   = 0
        result[score_col] = np.nan
        return

    # blockid20 is an array column — explode to one row per (dest_id, block_id)
    dest_exp = dest_df[["id", "blockid20"]].explode("blockid20").dropna(subset=["blockid20"])
    dest_exp = dest_exp.copy()
    dest_exp["blockid20"] = dest_exp["blockid20"].astype(str)

    ccb_d = ccb.merge(
        dest_exp.rename(columns={"blockid20": "target_blockid20"}),
        on="target_blockid20",
    )

    high = ccb_d.groupby("source_blockid20")["id"].nunique()
    low  = ccb_d[ccb_d["low_stress"]].groupby("source_blockid20")["id"].nunique()

    result[high_col]  = high.reindex(result.index).fillna(0).astype(int)
    result[low_col]   = low.reindex(result.index).fillna(0).astype(int)
    result[score_col] = _destination_score_vec(
        result[low_col].values,
        result[high_col].values,
        first, second, third, max_score,
    )


def _compute_trails_access(
    graph: GraphBundle,
    trails_df: pd.DataFrame | None,
    blocks_df: pd.DataFrame,
    result: pd.DataFrame,
    max_trip_distance: int,
    name: str = "trails",
    first: float = 0.7,
    second: float = 0.2,
    third: float = 0.0,
    max_score: float = 1.0,
) -> None:
    """Compute {name}_high_stress/_low_stress/_score via reverse Dijkstra (mutates result).

    Reverse Dijkstra: one call per qualifying trail on transposed graph finds which
    source blocks can reach it. O(n_trails × graph) — fast when n_trails << n_blocks.

    Scoring params (``first``/``second``/``third``/``max_score``) come from the active
    ``network_path`` catalog entry; the SQL-reference defaults reproduce the legacy
    ``0.7 / 0.2`` trail formula. When trails_df is empty/None, counts are 0, score NaN.
    """
    import scipy.sparse.csgraph as csg

    high_col  = f"{name}_high_stress"
    low_col   = f"{name}_low_stress"
    score_col = f"{name}_score"

    if trails_df is None or trails_df.empty:
        result[high_col] = 0
        result[low_col]  = 0
        result[score_col] = np.nan
        return

    n = graph.n_verts
    G_high_rev = graph.G_high.T.tocsr()
    G_low_rev  = graph.G_low.T.tocsr()

    block_road_map = dict(zip(blocks_df["geoid20"].astype(str), blocks_df["road_ids"]))
    src_geoids = list(result.index)

    src_verts_by_block = []
    for geoid in src_geoids:
        road_ids = block_road_map.get(str(geoid))
        if road_ids is None or len(road_ids) == 0:
            road_ids = []
        verts = np.array(
            [graph.road_to_vert[rid] for rid in road_ids
             if rid in graph.road_to_vert and rid in graph.boundary_road_ids],
            dtype=np.int32,
        )
        src_verts_by_block.append(verts)

    vert_to_block_idxs: list[list[int]] = [[] for _ in range(n)]
    for bidx, bverts in enumerate(src_verts_by_block):
        for v in bverts.tolist():
            vert_to_block_idxs[v].append(bidx)

    trail_high_counts = [0] * len(src_geoids)
    trail_low_counts  = [0] * len(src_geoids)

    for _, trail_row in trails_df.iterrows():
        road_ids = trail_row["road_ids"]
        if road_ids is None or len(road_ids) == 0:
            road_ids = []
        tverts = np.array(
            [graph.road_to_vert[rid] for rid in road_ids if rid in graph.road_to_vert],
            dtype=np.int32,
        )
        if len(tverts) == 0:
            continue

        d_high = csg.dijkstra(G_high_rev, indices=tverts, limit=max_trip_distance, directed=True)
        reached_high: set[int] = set()
        for v in np.where(d_high.min(axis=0) <= max_trip_distance)[0].tolist():
            reached_high.update(vert_to_block_idxs[v])
        for bidx in reached_high:
            trail_high_counts[bidx] += 1

        d_low = csg.dijkstra(G_low_rev, indices=tverts, limit=max_trip_distance, directed=True)
        reached_low: set[int] = set()
        for v in np.where(d_low.min(axis=0) <= max_trip_distance)[0].tolist():
            reached_low.update(vert_to_block_idxs[v])
        for bidx in reached_low:
            trail_low_counts[bidx] += 1

    result[high_col] = trail_high_counts
    result[low_col]  = trail_low_counts

    # Piecewise scoring identical to destinations (access_*.sql CASE); params from
    # the catalog network_path entry. Defaults (0.7/0.2) reproduce the legacy formula.
    result[score_col] = _destination_score_vec(
        np.array(trail_low_counts, dtype=float),
        np.array(trail_high_counts, dtype=float),
        first, second, third, max_score,
    )


def _weighted_category(
    result: pd.DataFrame,
    parts: list[tuple[float, str]],
) -> np.ndarray:
    """Weighted average with conditional denominator: only non-null scores count.

    Matches _weighted() in brokenspoke-analyzer compute.py:_python_access_all().
    """
    num = sum(w * result[col].fillna(0) for w, col in parts if col in result.columns)
    den = sum(
        w * result[col].notna().astype(float)
        for w, col in parts
        if col in result.columns
    )
    return np.where(den == 0, np.nan, num / den)


# ── Category membership helpers ───────────────────────────────────────────────

# employment (LODES jobs) is the only scoring member not in the destination catalog.
# It draws the intra-category weight left unallocated by the catalog destinations in
# its category, so no weight literal is hard-coded. Trails became a catalog
# `network_path` entry in Phase 34f and now flows through ``resolved_weights`` like any
# other recreation member.
_EMPLOYMENT_CATEGORY = "opportunity"   # non-gating: weighted but never gates inclusion


@dataclasses.dataclass
class _ScoreMember:
    """A weighted member of a scoring category.

    ``gating`` members participate in the category's reachability test (their
    ``high_stress`` counts toward inclusion). Non-gating members (employment)
    carry weight in the score but never decide whether the category is included.
    ``active`` toggles whether a non-gating member contributes its weight at all
    (employment is inactive when there is no jobs data).
    """

    name: str
    weight: float
    gating: bool
    active: bool = True

    @property
    def score_col(self) -> str:
        return f"{self.name}_score"

    @property
    def high_col(self) -> str:
        return f"{self.name}_high_stress"


def _get_col(result: pd.DataFrame, col: str) -> pd.Series:
    """Return ``result[col]`` or an all-zero Series when the column is absent."""
    if col in result.columns:
        return result[col]
    return pd.Series(0.0, index=result.index, dtype=float)


def _series_sum(terms: Iterable[pd.Series], index: pd.Index) -> pd.Series:
    """Sum an iterable of Series into a Series aligned on ``index`` (empty → zeros)."""
    acc = pd.Series(0.0, index=index)
    for t in terms:
        acc = acc + t
    return acc


def _add_pseudo(
    members: dict[str, list[_ScoreMember]],
    category: str,
    name: str,
    gating: bool,
    active: bool,
) -> None:
    """Append a non-catalog pseudo-member, weighted by the category's unallocated share."""
    allocated = sum(m.weight for m in members.get(category, []) if m.gating)
    weight = max(0.0, 1.0 - allocated)
    members[category].append(_ScoreMember(name, weight, gating=gating, active=active))


def _build_category_members(
    registry: DestinationRegistry | None, has_jobs: bool
) -> dict[str, list[_ScoreMember]]:
    """Map each active scoring category to its weighted members, from the catalog.

    Destination membership and intra-category weights come from
    ``registry.resolved_weights(category)`` (trails, a ``network_path`` entry, is one
    such member). employment is added as a non-catalog pseudo-member.
    """
    members: dict[str, list[_ScoreMember]] = defaultdict(list)
    categories: list[str] = []
    for dt in (registry.active() if registry is not None else []):
        if dt.scoring_category not in categories:
            categories.append(dt.scoring_category)
    for cat in categories:
        for dest_name, weight in registry.resolved_weights(cat).items():
            members[cat].append(_ScoreMember(dest_name, float(weight), gating=True))

    _add_pseudo(members, _EMPLOYMENT_CATEGORY, "emp", gating=False, active=has_jobs)
    return members


def compute_scores(
    connectivity_df: pd.DataFrame,
    blocks_df: pd.DataFrame,
    destinations: dict[str, pd.DataFrame],
    jobs_df: pd.DataFrame | None,
    trails_df: pd.DataFrame | None,
    graph: GraphBundle | None,
    config: BNAConfig,
) -> pd.DataFrame:
    """Compute per-block access scores from connectivity, census, and destination data.

    Returns one row per block (all blocks in blocks_df). Blocks with no connectivity
    get NULL scores.

    Args:
        connectivity_df: Output of compute_connectivity — (source, target, low_stress, costs).
        blocks_df: Census block table with geoid20, pop20, road_ids.
        destinations: Mapping dest_name → DataFrame with id and blockid20 (array) columns.
            Empty dict or empty DataFrames = no destination access (score NULL).
        jobs_df: LODES jobs by block: blockid20, jobs. Empty = employment columns NULL.
        trails_df: Qualifying trail paths: path_id, road_ids, path_length, bbox_length.
            Empty = trails columns all 0 / NULL.
        graph: GraphBundle with G_high, G_low, road_to_vert, boundary_road_ids.
            Required for trails reverse Dijkstra; None = trails skipped.
        config: Pipeline configuration (scoring weights, population params).
    """
    destinations = destinations or {}
    _logger.info(
        "scores  input — connectivity_pairs=%d  blocks=%d  dest_types=%d  has_jobs=%s  has_trails=%s",
        len(connectivity_df), len(blocks_df), len(destinations),
        jobs_df is not None and not jobs_df.empty,
        trails_df is not None and not trails_df.empty,
    )

    # ── Source blocks ────────────────────────────────────────────────────────────
    # All census blocks appear in the result; blocks with no connectivity get NULLs.
    all_geoids = blocks_df["geoid20"].astype(str).tolist()
    result = pd.DataFrame(index=pd.Index(all_geoids, name="geoid20"))

    ccb = connectivity_df[["source_blockid20", "target_blockid20", "low_stress"]].copy()
    ccb["low_stress"] = ccb["low_stress"].astype(bool)

    # ── Population ───────────────────────────────────────────────────────────────
    target_pop = blocks_df.set_index("geoid20")["pop20"]
    ccb["target_pop"] = ccb["target_blockid20"].map(target_pop)

    pop_high = ccb.groupby("source_blockid20")["target_pop"].sum()
    pop_low  = ccb[ccb["low_stress"]].groupby("source_blockid20")["target_pop"].sum()

    result["pop_high_stress"] = pop_high.reindex(result.index)
    result["pop_low_stress"]  = pop_low.reindex(result.index).fillna(0).astype(int)

    p = config.scoring.population
    result["pop_score"] = _piecewise_score(
        result["pop_high_stress"].to_numpy(dtype=float, na_value=np.nan),
        result["pop_low_stress"].to_numpy(dtype=float),
        p.max_score, p.step1, p.score1, p.step2, p.score2, p.step3, p.score3,
    )

    _logger.info(
        "scores  population — pop_high_stress non-null=%d  pop_low_stress non-null=%d",
        int(result["pop_high_stress"].notna().sum()),
        int(result["pop_low_stress"].notna().sum()),
    )

    # ── Employment ───────────────────────────────────────────────────────────────
    if jobs_df is not None and not jobs_df.empty:
        target_jobs = jobs_df.set_index("blockid20")["jobs"]
        ccb["target_jobs"] = ccb["target_blockid20"].map(target_jobs).fillna(0)
        emp_high = ccb.groupby("source_blockid20")["target_jobs"].sum()
        emp_low  = ccb[ccb["low_stress"]].groupby("source_blockid20")["target_jobs"].sum()
        result["emp_high_stress"] = emp_high.reindex(result.index)
        result["emp_low_stress"]  = emp_low.reindex(result.index).fillna(0).astype(int)
        result["emp_score"] = _piecewise_score(
            result["emp_high_stress"].to_numpy(dtype=float, na_value=np.nan),
            result["emp_low_stress"].to_numpy(dtype=float),
            p.max_score, p.step1, p.score1, p.step2, p.score2, p.step3, p.score3,
        )
    else:
        result["emp_high_stress"] = np.nan
        result["emp_low_stress"]  = np.nan
        result["emp_score"]       = np.nan

    # ── Destinations ─────────────────────────────────────────────────────────────
    registry = config.destinations
    validate_scoring_categories(config.scoring, registry)
    dest_types = registry.active() if registry is not None else []

    for dest_type in dest_types:
        if dest_type.type == "network_path":
            continue  # linear network features (trails) are scored below, not as POIs
        dest_df = destinations.get(dest_type.name)
        s = dest_type.scoring
        _compute_destination_access(
            ccb, dest_df, result, dest_type.name,
            s.first, s.second, s.third, s.max_score,
        )

    # ── Trails (network_path pseudo-destination) ──────────────────────────────────
    # Detection (segment.py), scoring params and recreation membership/weight all come
    # from the active `network_path` catalog entry (Phase 34f). Fallback columns when
    # no entry is active or the graph is unavailable.
    network_path_entry = next(
        (dt for dt in dest_types if dt.type == "network_path"), None
    )
    if graph is not None and network_path_entry is not None:
        sp = network_path_entry.scoring
        _compute_trails_access(
            graph, trails_df, blocks_df, result, config.max_trip_distance,
            name=network_path_entry.name,
            first=sp.first, second=sp.second, third=sp.third, max_score=sp.max_score,
        )
    else:
        tname = network_path_entry.name if network_path_entry is not None else "trails"
        result[f"{tname}_high_stress"] = 0
        result[f"{tname}_low_stress"]  = 0
        result[f"{tname}_score"]       = np.nan

    # ── Category membership & weights (derived from the active catalog) ───────────
    # Which destination types belong to which category and their intra-category
    # weights come entirely from the DestinationRegistry — including trails, a
    # `network_path` recreation member since Phase 34f. employment (LODES jobs) is the
    # only scoring member still folded in as a non-catalog pseudo-member.
    has_jobs = jobs_df is not None and not jobs_df.empty
    members = _build_category_members(registry, has_jobs)

    # Category score = weighted average over members, conditional notna denominator
    # (matches compute.py:_python_access_all() _weighted()).
    for cat, mem in members.items():
        result[f"{cat}_score"] = _weighted_category(
            result, [(m.weight, m.score_col) for m in mem]
        )

    # ── Overall score: uniform reachability-based inclusion (Phase 34e) ───────────
    # One rule for every destination category, with no hard-coded category names:
    #
    #     any_reachable[cat] = (sum of high_stress over the category's gating members) > 0
    #     overall = ( people_w*pop_score + sum_cat cat_w[cat]*cat_contrib*any_reachable )
    #             / ( people_w            + sum_cat cat_w[cat]*any_reachable )
    #
    # `people` (population) is always included, so overall_den >= people_w > 0 and the
    # denominator is never zero — the np.where guard at the end is therefore defensive
    # only. Inter-category weights come from scoring.category_weights.
    #
    # Inclusion is evaluated PER BLOCK: any_reachable is a per-block boolean, matching
    # brokenspoke-analyzer (compute.py:_*_contrib / access_overall.sql), which gates
    # each block's overall score on what that block can reach. Decision 2 phrases the
    # rule as "reachable from any block"; the reference is per-block and parity is
    # ground truth — see spec-issues/RESOLVED-34e-perblock-inclusion.md.
    #
    # Non-gating members (employment) carry weight in cat_contrib but never gate
    # inclusion, and contribute their weight to the per-category denominator
    # unconditionally while active. That is why cat_contrib is recomputed here rather
    # than reusing the precomputed `{cat}_score` column: that column's conditional
    # notna() denominator would drop the employment weight on blocks with no reachable
    # jobs and break parity with the reference.
    cw = config.scoring.category_weights
    people_w = config.scoring.people

    overall_num = people_w * _get_col(result, "pop_score").fillna(0)
    overall_den = pd.Series(float(people_w), index=result.index)

    for cat, mem in members.items():
        inter_w = cw[cat]
        gating = [m for m in mem if m.gating]
        any_reachable = _series_sum(
            (_get_col(result, m.high_col).fillna(0) for m in gating),
            result.index,
        ) > 0
        cat_num = _series_sum(
            (m.weight * _get_col(result, m.score_col).fillna(0) for m in mem),
            result.index,
        )
        cat_den = _series_sum(
            (m.weight * (_get_col(result, m.high_col).fillna(0) > 0).astype(float)
             for m in gating),
            result.index,
        )
        cat_den = cat_den + sum(m.weight for m in mem if not m.gating and m.active)
        cat_contrib = np.where(
            any_reachable, cat_num / np.where(cat_den == 0, 1, cat_den), 0.0
        )
        overall_num = overall_num + inter_w * cat_contrib
        overall_den = overall_den + inter_w * any_reachable.astype(float)

    result["overall_score"] = np.where(
        overall_den == 0, np.nan,
        100.0 * overall_num / overall_den,
    )

    _overall = result["overall_score"].dropna()
    _logger.info(
        "scores  output — blocks=%d  scored=%d  overall_mean=%.1f",
        len(result), len(_overall), float(_overall.mean()) if len(_overall) else 0.0,
    )
    return result.reset_index()


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

import pickle  # noqa: E402
from pathlib import Path  # noqa: E402

from bikescore_bna.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd

    conn_dir = Path(input_paths["connectivity"])
    dest_dir = Path(input_paths["destinations"])
    jobs_dir = Path(input_paths["jobs"])
    graph_dir = Path(input_paths["graph"])
    segment_dir = Path(input_paths["segment"])

    connectivity_df = pd.read_parquet(conn_dir / "connectivity.parquet")
    blocks_gdf = gpd.read_parquet(graph_dir / "blocks_with_roads.parquet")

    destinations = {}
    for f in sorted(dest_dir.glob("dest_*.parquet")):
        name = f.stem[5:]  # strip "dest_" prefix
        destinations[name] = pd.read_parquet(f)

    jobs_df = pd.read_parquet(jobs_dir / "jobs.parquet")
    trails_df = pd.read_parquet(segment_dir / "trails.parquet")

    with open(graph_dir / "graph_bundle.pkl", "rb") as f:
        graph_bundle = pickle.load(f)

    qualifying_trails = (
        trails_df[
            (trails_df["path_length"] >= config.min_path_length)
            & (trails_df["bbox_length"] >= config.min_bbox_length)
        ]
        if not trails_df.empty
        else trails_df
    )

    result = compute_scores(
        connectivity_df,
        blocks_gdf,
        destinations,
        jobs_df if not jobs_df.empty else None,
        qualifying_trails if not qualifying_trails.empty else None,
        graph_bundle,
        config,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.to_parquet(out / "scores.parquet")


SCORES = StageSpec(
    name="scores",
    depends_on=("connectivity", "destinations", "jobs", "graph", "segment"),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
