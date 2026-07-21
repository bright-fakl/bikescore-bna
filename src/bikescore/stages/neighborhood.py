"""Neighborhood stage: city-level aggregate scores and mileage.

Produces the 23-row neighborhood_overall_scores table, the 132-row
neighborhood_score_inputs table, and the mileage table.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from bikescore.config import BNAConfig
from bikescore.destinations import DestinationType, default_destination_registry

STAGE_VERSION: str = "1.0.0"

# ── Standard-type constants ───────────────────────────────────────────────────

_logger = logging.getLogger("bikescore")

# Maps destination name → use_* column used in score_inputs for standard types.
# Custom types use use_{name} directly.
_STANDARD_USE_KEY: dict[str, str] = {
    "schools": "use_k12", "colleges": "use_tech", "universities": "use_univ",
    "doctors": "use_doctor", "dentists": "use_dentist", "hospitals": "use_hospital",
    "pharmacies": "use_pharmacy", "retail": "use_retail", "supermarkets": "use_grocery",
    "social_services": "use_social_svcs", "parks": "use_parks",
    "community_centers": "use_comm_ctrs", "transit": "use_transit",
}

_CATEGORY_DISPLAY: dict[str, str] = {
    "opportunity": "Opportunity",
    "core_services": "Core Services",
    "recreation": "Recreation",
    "retail": "Retail",
    "transit": "Transit",
}


# ── Score helpers ─────────────────────────────────────────────────────────────

def _pct_ratio(low: pd.Series, high: pd.Series, q: float) -> float:
    ratio = low.astype(float) / high.where(high > 0, np.nan)
    if ratio.isna().all():
        return np.nan
    return float(ratio.quantile(q, interpolation="linear"))


def _avg_ratio_float(low: pd.Series, high: pd.Series) -> float:
    """Float division; used for population and employment."""
    h = float(high.sum())
    return float(low.sum()) / h if h > 0 else 0.0


def _avg_ratio_int(low: pd.Series, high: pd.Series) -> float:
    """Integer division; matches PostgreSQL int/int for destination counts."""
    h = int(high.sum())
    return float(int(low.sum()) // h) if h > 0 else 0.0


def _pop_weighted(pop: pd.Series, score: pd.Series, denom: float) -> float:
    if denom == 0:
        return 0.0
    return float((pop * score).sum()) / denom


def _shed_avg(dest_df: pd.DataFrame | None) -> float:
    if dest_df is None or dest_df.empty:
        return np.nan
    h = float(dest_df["pop_high_stress"].sum())
    if h == 0:
        return np.nan
    return float(dest_df["pop_low_stress"].sum()) / h


def _shed_pct(dest_df: pd.DataFrame | None, q: float) -> float:
    if dest_df is None or dest_df.empty:
        return np.nan
    ratio = dest_df["pop_low_stress"].astype(float) / dest_df["pop_high_stress"].where(
        dest_df["pop_high_stress"] > 0, np.nan
    )
    if ratio.isna().all():
        return np.nan
    return float(ratio.quantile(q, interpolation="linear"))


# ── tmp_pop denominators ──────────────────────────────────────────────────────

def _tmp_pop(merged: pd.DataFrame) -> dict[str, float]:
    result: dict[str, float] = {"overall": float(merged["pop20"].sum())}
    if "trails_high_stress" in merged.columns:
        result["trails"] = float(merged.loc[merged["trails_high_stress"] > 0, "pop20"].sum())
    else:
        result["trails"] = 0.0
    return result


# ── Use-flag builder ──────────────────────────────────────────────────────────

def _use_flags(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {k: None for k in (
        "use_pop", "use_emp", "use_k12", "use_tech", "use_univ",
        "use_doctor", "use_dentist", "use_hospital", "use_pharmacy", "use_retail",
        "use_grocery", "use_social_svcs", "use_parks", "use_trails", "use_comm_ctrs",
        "use_transit",
    )}
    base.update(kwargs)
    return base


def _row(
    row_id: int, category: str, score_name: str, score: float,
    notes: str, human_explanation: str, use_flag: str | None = None,
) -> dict[str, Any]:
    # Round to 4 dp to match PostgreSQL NUMERIC(16,4) storage precision
    if score is not None and not (isinstance(score, float) and np.isnan(score)):
        score = round(float(score), 4)
    d = {
        "id": row_id, "category": category, "score_name": score_name,
        "score": score, "notes": notes, "human_explanation": human_explanation,
    }
    d.update(_use_flags(**({use_flag: True} if use_flag else {})))
    return d


# ── Score-input row builders ──────────────────────────────────────────────────

def _rows_people(m: pd.DataFrame, tp: dict) -> list[dict]:
    L, H = m["pop_low_stress"].astype(float), m["pop_high_stress"].astype(float)
    return [
        _row(1, "People", "Median score of access to population",
             _pct_ratio(L, H, 0.5),
             "Score of population accessible by low stress to population accessible overall, expressed as the median of all census blocks in the neighborhood",
             "Half of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, half have a lower ratio."),
        _row(2, "People", "70th percentile score of access to population",
             _pct_ratio(L, H, 0.7),
             "Score of population accessible by low stress to population accessible overall, expressed as the 70th percentile of all census blocks in the neighborhood",
             "30% of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, 70% have a lower ratio."),
        _row(3, "People", "30th percentile score of access to population",
             _pct_ratio(L, H, 0.3),
             "Score of population accessible by low stress to population accessible overall, expressed as the 30th percentile of all census blocks in the neighborhood",
             "70% of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, 30% have a lower ratio."),
        _row(4, "People", "Average score of access to population",
             _avg_ratio_float(L, H),
             "Score of population accessible by low stress to population accessible overall, expressed as the average of all census blocks in the neighborhood",
             "On average, census blocks in the neighborhood have this ratio of low stress to high stress access."),
        _row(5, "People", "Average score of access to population",
             _pop_weighted(m["pop20"], m["pop_score"], tp["overall"]),
             "Average population score for census blocks weighted by population.",
             "On average, census blocks in the neighborhood received this population score.",
             "use_pop"),
    ]


def _rows_employment(m: pd.DataFrame, tp: dict) -> list[dict]:
    L, H = m["emp_low_stress"].astype(float), m["emp_high_stress"].astype(float)
    return [
        _row(6, "Opportunity", "Median score of access to employment",
             _pct_ratio(L, H, 0.5),
             "Score of employment accessible by low stress to employment accessible overall, expressed as the median of all census blocks in the neighborhood",
             "Half of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, half have a lower ratio."),
        _row(7, "Opportunity", "70th percentile score of access to employment",
             _pct_ratio(L, H, 0.7),
             "Score of employment accessible by low stress to employment accessible overall, expressed as the 70th percentile of all census blocks in the neighborhood",
             "30% of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, 70% have a lower ratio."),
        _row(8, "Opportunity", "30th percentile score of access to employment",
             _pct_ratio(L, H, 0.3),
             "Score of employment accessible by low stress to employment accessible overall, expressed as the 30th percentile of all census blocks in the neighborhood",
             "70% of all census blocks in the neighborhood have a ratio of low stress to high stress access above this number, 30% have a lower ratio."),
        _row(9, "Opportunity", "Average score of access to employment",
             _avg_ratio_float(L, H),
             "Score of employment accessible by low stress to employment accessible overall, expressed as the average of all census blocks in the neighborhood",
             "On average, census blocks in the neighborhood have this ratio of low stress to high stress access."),
        _row(10, "Opportunity", "Average score of access to jobs",
             _pop_weighted(m["pop20"], m["emp_score"], tp["overall"]),
             "Average employment score for census blocks weighted by population.",
             "On average, census blocks in the neighborhood received this employment score.",
             "use_emp"),
    ]


def _rows_trails(m: pd.DataFrame, tp: dict) -> list[dict]:
    L, H = m["trails_low_stress"].astype(float), m["trails_high_stress"].astype(float)
    return [
        _row(110, "Recreation", "Average score of low stress access to trails",
             _avg_ratio_int(L, H),
             "Number of trails accessible by low stress expressed as an average of all census blocks in the neighborhood",
             "On average, census blocks in the neighborhood have low stress access to this many trails."),
        _row(111, "Recreation", "Median score of trails access",
             _pct_ratio(L, H, 0.5),
             "Score of trails accessible by low stress compared to trails accessible by high stress expressed as the median of all census blocks in the neighborhood",
             "Half of census blocks in this neighborhood have low stress access to a higher ratio of trails within biking distance, half have access to a lower ratio."),
        _row(112, "Recreation", "70th percentile score of trails access",
             _pct_ratio(L, H, 0.7),
             "Score of trails accessible by low stress compared to trails accessible by high stress expressed as the 70th percentile of all census blocks in the neighborhood",
             "30% of census blocks in this neighborhood have low stress access to a higher ratio of trails within biking distance, 70% have access to a lower ratio."),
        _row(113, "Recreation", "30th percentile score of trails access",
             _pct_ratio(L, H, 0.3),
             "Score of trails accessible by low stress compared to trails accessible by high stress expressed as the 30th percentile of all census blocks in the neighborhood",
             "70% of census blocks in this neighborhood have low stress access to a higher ratio of trails within biking distance, 30% have access to a lower ratio."),
        _row(114, "Recreation", "Average score of access to trails",
             _pop_weighted(m["pop20"], m["trails_score"], tp["trails"]),
             "Average trails score for census blocks weighted by population.",
             "On average, census blocks in the neighborhood received this trails score.",
             "use_trails"),
    ]


def _rows_for_destination(
    dest_type: DestinationType,
    merged: pd.DataFrame,
    destinations: dict[str, pd.DataFrame],
    start_row_id: int,
) -> list[dict]:
    """Generate the 9 standard score_inputs rows for a destination type.

    Uses score_noun, score_noun_singular, score_noun_shed, and related fields on
    DestinationType to reproduce the exact reference text. Standard types set these
    fields explicitly; custom types rely on sensible defaults.
    """
    name = dest_type.name
    cat = _CATEGORY_DISPLAY.get(dest_type.scoring_category, dest_type.scoring_category.title())
    use_flag = _STANDARD_USE_KEY.get(name, f"use_{name}")

    dn = dest_type.score_noun or dest_type.display_name
    dn_s = dest_type.score_noun_singular or dn
    dn_acc = dest_type.score_noun_access or dest_type.human_explanation or dn
    he = dest_type.human_explanation or dn

    L = merged[f"{name}_low_stress"].astype(float)
    H = merged[f"{name}_high_stress"].astype(float)
    D = destinations.get(name)
    tp_pop = float(merged.loc[merged[f"{name}_high_stress"] > 0, "pop20"].sum())

    r = start_row_id
    return [
        _row(r+0, cat, f"Average score of low stress access to {dn}",
             _avg_ratio_int(L, H),
             f"Number of {dn} accessible by low stress expressed as an average of all census blocks in the neighborhood",
             f"On average, census blocks in the neighborhood have low stress access to this many {dn}."),
        _row(r+1, cat, f"Median score of {dn_s} access",
             _pct_ratio(L, H, 0.5),
             f"Score of {dn} accessible by low stress compared to {dn} accessible by high stress expressed as the median of all census blocks in the neighborhood",
             f"Half of census blocks in this neighborhood have low stress access to a higher ratio of {dn} within biking distance, half have access to a lower ratio."),
        _row(r+2, cat, f"70th percentile score of {dn_s} access",
             _pct_ratio(L, H, 0.7),
             f"Score of {dn} accessible by low stress compared to {dn} accessible by high stress expressed as the 70th percentile of all census blocks in the neighborhood",
             f"30% of census blocks in this neighborhood have low stress access to a higher ratio of {dn} within biking distance, 70% have access to a lower ratio."),
        _row(r+3, cat, f"30th percentile score of {dn_s} access",
             _pct_ratio(L, H, 0.3),
             f"Score of {dn} accessible by low stress compared to {dn} accessible by high stress expressed as the 30th percentile of all census blocks in the neighborhood",
             f"70% of census blocks in this neighborhood have low stress access to a higher ratio of {dn} within biking distance, 30% have access to a lower ratio."),
        _row(r+4, cat, f"Average score of access to {dn_acc}",
             _pop_weighted(merged["pop20"], merged[f"{name}_score"], tp_pop),
             f"Average {he} score for census blocks weighted by population.",
             f"On average, census blocks in the neighborhood received this {he} score.",
             use_flag),
        _row(r+5, cat, f"Average {dn_s} bike shed access score",
             _shed_avg(D),
             f"Score of population with low stress access compared to total population within the bike shed distance of {dn} in the neighborhood expressed as an average of all {dn} in the neighborhood",
             f"On average, {dn} in the neighborhood are connected by the low stress access to this percentage people within biking distance."),
        _row(r+6, cat, f"Median {dn_s} population shed score",
             _shed_pct(D, 0.5),
             f"Score of population with low stress access to {dn} in the neighborhood to total population within the bike shed of each {dn_s} expressed as a median of all {dn} in the neighborhood",
             f"Half of {dn} in the neighborhood have low stress connections to a higher percentage of people within biking distance, half are connected to a lower percentage."),
        _row(r+7, cat, f"70th percentile {dn_s} population shed score",
             _shed_pct(D, 0.7),
             f"Score of population with low stress access to {dn} in the neighborhood to total population within the bike shed of each {dn_s} expressed as the 70th percentile of all {dn} in the neighborhood",
             f"30% of {dn} in the neighborhood have low stress connections to a higher percentage of people within biking distance, 70% are connected to a lower percentage."),
        _row(r+8, cat, f"30th percentile {dn_s} population shed score",
             _shed_pct(D, 0.3),
             f"Score of population with low stress access to {dn} in the neighborhood to total population within the bike shed of each {dn_s} expressed as the 30th percentile of all {dn} in the neighborhood",
             f"70% of {dn} in the neighborhood have low stress connections to a higher percentage of people within biking distance, 30% are connected to a lower percentage."),
    ]


_STANDARD_ROW_IDS: dict[str, int] = {
    "schools": 11, "colleges": 20, "universities": 29,
    "doctors": 38, "dentists": 47, "hospitals": 56,
    "pharmacies": 65, "retail": 74, "supermarkets": 83,
    "social_services": 92, "parks": 101, "community_centers": 115,
    "transit": 124,
}


def _build_score_inputs(
    merged: pd.DataFrame,
    destinations: dict[str, pd.DataFrame],
    tmp_pop: dict[str, float],
    config: BNAConfig,
) -> pd.DataFrame:
    dest_registry = config.destinations if config.destinations is not None else default_destination_registry()
    active_by_name = {dt.name: dt for dt in dest_registry.active()}

    rows: list[dict] = []
    rows += _rows_people(merged, tmp_pop)
    rows += _rows_employment(merged, tmp_pop)

    # Emit standard types in fixed ID order (sorted by start row).
    for name, start in sorted(_STANDARD_ROW_IDS.items(), key=lambda kv: kv[1]):
        if name not in active_by_name:
            continue
        dest_type = active_by_name[name]
        if f"{name}_high_stress" not in merged.columns:
            continue
        rows += _rows_for_destination(dest_type, merged, destinations, start)
        if name == "parks" and "trails_high_stress" in merged.columns:
            rows += _rows_trails(merged, tmp_pop)  # trails always follows parks (IDs 110-114)

    # Custom destination types (non-standard) — rows 133+
    next_id = 133
    for dest_type in dest_registry.active():
        if (dest_type.name in _STANDARD_ROW_IDS
                or dest_type.type == "network_path"):
            continue
        if f"{dest_type.name}_high_stress" not in merged.columns:
            continue
        rows += _rows_for_destination(dest_type, merged, destinations, next_id)
        next_id += 9

    return pd.DataFrame(rows)


# ── Overall scores ────────────────────────────────────────────────────────────

def _category_score(
    sub_scores: dict[str, float],
    presence: dict[str, bool],
    weights: dict[str, float],
) -> float | None:
    if not any(presence.values()):
        return None
    denom = sum(w for k, w in weights.items() if presence.get(k, False))
    if denom == 0:
        return None
    return sum(weights[k] * sub_scores[k] for k in weights if presence.get(k, False)) / denom


def _build_overall_scores(
    score_inputs: pd.DataFrame,
    merged: pd.DataFrame,
    segments_df: pd.DataFrame,
    boundary_geom: Any | None,
    config: BNAConfig,
) -> pd.DataFrame:
    dest_registry = config.destinations if config.destinations is not None else default_destination_registry()

    # Derive score and human_explanation from the use_* flag columns in score_inputs.
    _sc: dict[str, float] = {}
    _hu: dict[str, str | None] = {}
    for key in (c for c in score_inputs.columns if c.startswith("use_")):
        flagged = score_inputs[score_inputs[key].eq(True)]
        if len(flagged) > 0:
            _sc[key] = float(flagged["score"].iloc[0])
            _hu[key] = str(flagged["human_explanation"].iloc[0])
        else:
            _sc[key] = 0.0
            _hu[key] = None

    def _has(col: str) -> bool:
        return bool((merged[col] > 0).any()) if col in merged.columns else False

    # ── Per-destination scores from the registry ──────────────────────────────
    # cat_sub[category][name] = score; cat_pres[category][name] = presence bool.
    cat_sub: dict[str, dict[str, float]] = {}
    cat_pres: dict[str, dict[str, bool]] = {}

    # Every catalog member contributes, including the `trails` network_path entry
    # (its score lives in the `trails_*` columns and its weight in the recreation
    # catalog weights — no special-casing, no hard-coded literal).
    for dt in dest_registry.active():
        use_flag = _STANDARD_USE_KEY.get(dt.name, f"use_{dt.name}")
        s = _sc.get(use_flag, 0.0)
        pres = _has(f"{dt.name}_high_stress")
        cat_sub.setdefault(dt.scoring_category, {})[dt.name] = s
        cat_pres.setdefault(dt.scoring_category, {})[dt.name] = pres

    # Employment is a demographic score (LODES jobs), not a catalog DestinationType.
    # It takes the opportunity weight left unallocated by the catalog opportunity types
    # — derived, not hard-coded — mirroring scores.py:_build_category_members.
    _EMP_WEIGHT = max(
        0.0, 1.0 - sum(dest_registry.resolved_weights("opportunity").values())
    )
    emp_s = _sc.get("use_emp", 0.0)
    trails_s = _sc.get("use_trails", 0.0)  # for the recreation_trails row below
    cat_sub.setdefault("opportunity", {})["__emp__"] = emp_s
    cat_pres.setdefault("opportunity", {})["__emp__"] = _has("emp_high_stress")

    # ── Category scores ───────────────────────────────────────────────────────
    def _score_cat(cat: str) -> float | None:
        sub = cat_sub.get(cat, {})
        pres = cat_pres.get(cat, {})
        if not any(pres.values()):
            return None
        weights = dict(dest_registry.resolved_weights(cat))
        if cat == "opportunity":
            weights["__emp__"] = _EMP_WEIGHT
        return _category_score(sub, pres, weights)

    # ── Fixed scores ──────────────────────────────────────────────────────────
    people_s = _sc.get("use_pop", 0.0)
    denom_pop = float(merged.loc[merged["reachable_blocks"] > 0, "pop20"].sum())
    eligible  = merged[(merged["pop20"] > 0) & (merged["reachable_blocks"] > 0)]
    overall   = float((eligible["overall_score"] / 100.0 * eligible["pop20"] / denom_pop).sum())
    pop_total = float(merged["pop20"].sum())
    low_miles, high_miles = _compute_total_miles(segments_df, boundary_geom)

    # ── Assemble rows in BNA reference order ──────────────────────────────────
    # Category display order matches the SQL reference.
    _CAT_ORDER = ("opportunity", "core_services", "retail", "recreation", "transit")

    rows: list[dict] = [
        {"score_id": "people",                "score_original": people_s, "human_explanation": _hu.get("use_pop")},
        {"score_id": "opportunity_employment","score_original": emp_s,    "human_explanation": _hu.get("use_emp")},
    ]

    for cat in _CAT_ORDER:
        cat_dests = [
            dt for dt in dest_registry.active()
            if dt.scoring_category == cat and dt.type != "network_path"
        ]

        for dt in cat_dests:
            use_flag = _STANDARD_USE_KEY.get(dt.name, f"use_{dt.name}")
            sid = dt.score_id or f"{cat}_{dt.name}"
            rows.append({
                "score_id": sid,
                "score_original": _sc.get(use_flag, 0.0),
                "human_explanation": _hu.get(use_flag),
            })
            # Trails follow parks immediately (synthetic score, injected in-order).
            if dt.name == "parks":
                rows.append({
                    "score_id": "recreation_trails",
                    "score_original": trails_s,
                    "human_explanation": _hu.get("use_trails"),
                })

        # Emit a category aggregate for multi-member categories, and always for
        # opportunity (employment always participates) and recreation (trails always do).
        if len(cat_dests) > 1 or cat in ("opportunity", "recreation"):
            rows.append({"score_id": cat, "score_original": _score_cat(cat), "human_explanation": None})

    rows += [
        {"score_id": "overall_score",         "score_original": overall,    "human_explanation": None},
        {"score_id": "population_total",       "score_original": pop_total,  "human_explanation": "Total population of boundary"},
        {"score_id": "total_miles_low_stress", "score_original": low_miles,  "human_explanation": "Total low-stress miles"},
        {"score_id": "total_miles_high_stress","score_original": high_miles, "human_explanation": "Total high-stress miles"},
    ]

    df = pd.DataFrame(rows)
    df["score_normalized"] = df["score_original"] * 100.0
    df.loc[df["score_id"].isin(["total_miles_low_stress", "total_miles_high_stress"]),
           "score_normalized"] = df.loc[
        df["score_id"].isin(["total_miles_low_stress", "total_miles_high_stress"]),
        "score_original",
    ].round(1)
    df.loc[df["score_id"] == "population_total", "score_normalized"] = None
    return df[["score_id", "score_original", "score_normalized", "human_explanation"]]


# ── Mileage ───────────────────────────────────────────────────────────────────

def _compute_total_miles(
    segments_df: pd.DataFrame,
    boundary_geom: Any | None,
) -> tuple[float, float]:
    """Compute total low-stress and high-stress miles clipped to boundary."""
    ft = segments_df["ft_seg_stress"].fillna(0)
    tf = segments_df["tf_seg_stress"].fillna(0)
    stress_sum = ft + tf

    ls_mask = (ft == 1) | (tf == 1)
    hs_mask = (ft == 3) | (tf == 3)

    ls_mult = pd.Series(0.0, index=segments_df.index)
    ls_mult[stress_sum == 2] = 2.0
    ls_mult[(stress_sum == 4) | (stress_sum == 1)] = 1.0

    hs_mult = pd.Series(0.0, index=segments_df.index)
    hs_mult[stress_sum == 6] = 2.0
    hs_mult[(stress_sum == 4) | (stress_sum == 3)] = 1.0

    if boundary_geom is not None and "geometry_wkt" in segments_df.columns:
        lengths = _clip_lengths(segments_df, boundary_geom, ls_mask | hs_mask)
    else:
        lengths = segments_df.get("length_m", pd.Series(0.0, index=segments_df.index))

    ls_total = float((lengths[ls_mask] * ls_mult[ls_mask]).sum()) / 1609.34
    hs_total = float((lengths[hs_mask] * hs_mult[hs_mask]).sum()) / 1609.34
    return ls_total, hs_total


def _clip_lengths(
    segments_df: pd.DataFrame,
    boundary_geom: Any,
    mask: pd.Series,
) -> pd.Series:
    """Clip each segment geometry to boundary_geom and return length in metres."""
    try:
        from shapely import from_wkt
        from shapely.measurement import length as shp_length
    except ImportError:
        return segments_df.get("length_m", pd.Series(0.0, index=segments_df.index))

    lengths = pd.Series(0.0, index=segments_df.index)
    subset = segments_df[mask]
    for idx, row in subset.iterrows():
        try:
            geom = from_wkt(row["geometry_wkt"])
            clipped = geom.intersection(boundary_geom)
            lengths[idx] = shp_length(clipped)
        except Exception:
            lengths[idx] = float(row.get("length_m", 0.0) or 0.0)
    return lengths


def _compute_mileage(segments_df: pd.DataFrame) -> pd.DataFrame:
    """Compute mileage by bike infrastructure type (matches calculate_mileage.sql).

    Each segment contributes up to three rows (ft_bike_infra, tf_bike_infra, path).
    Path requires functional_class='path' AND xwalk IS NULL (or xwalk column absent).
    """
    if segments_df.empty:
        return pd.DataFrame(columns=["feature_type", "total_mileage"])

    valid_types = {"sharrow", "buffered_lane", "lane", "track", "path"}
    length_m = segments_df["length_m"].fillna(0.0).astype(float)

    parts: list[pd.DataFrame] = []

    for col in ("ft_bike_infra", "tf_bike_infra"):
        if col in segments_df.columns:
            mask = segments_df[col].isin(valid_types)
            if mask.any():
                parts.append(pd.DataFrame({
                    "feature_type": segments_df.loc[mask, col].values,
                    "total_mileage": length_m[mask].values,
                }))

    path_mask = segments_df.get("functional_class") == "path"
    if isinstance(path_mask, pd.Series) and path_mask.any():
        if "xwalk" in segments_df.columns:
            path_mask = path_mask & segments_df["xwalk"].isna()
        if path_mask.any():
            parts.append(pd.DataFrame({
                "feature_type": ["path"] * int(path_mask.sum()),
                "total_mileage": length_m[path_mask].values,
            }))

    if not parts:
        return pd.DataFrame(columns=["feature_type", "total_mileage"])

    result = (
        pd.concat(parts, ignore_index=True)
        .groupby("feature_type", as_index=False)["total_mileage"]
        .sum()
    )
    result["total_mileage"] = result["total_mileage"] / 1609.34
    return result.sort_values("feature_type").reset_index(drop=True)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_neighborhood(
    block_scores_df: pd.DataFrame,
    blocks_df: pd.DataFrame,
    segments_df: pd.DataFrame,
    destinations: dict[str, pd.DataFrame],
    config: BNAConfig,
    *,
    boundary_geom: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """Compute city-level aggregate scores and mileage.

    Args:
        block_scores_df: Per-block scores from compute_scores.
        blocks_df: Census blocks with geoid20, pop20, reachable_blocks.
        segments_df: Road segments with ft_bike_infra, tf_bike_infra, functional_class,
            ft_seg_stress, tf_seg_stress, length_m, geometry_wkt (optional), xwalk (optional).
        destinations: Mapping dest_name → DataFrame with pop_low_stress, pop_high_stress.
        config: Pipeline configuration.
        boundary_geom: Optional shapely geometry for total_miles boundary clipping.
            If None, uses full segment length (slight overestimate for buffer-zone roads).

    Returns:
        dict with "overall_scores" (23 rows), "score_inputs" (132+ rows), "mileage".
        Custom destination types contribute 9 additional rows each (IDs 133, 142, …).
    """
    destinations = destinations or {}
    _logger.info(
        "neighborhood  input — blocks=%d  dest_types=%d",
        len(blocks_df), len(destinations),
    )

    merged = blocks_df[["geoid20", "pop20", "reachable_blocks"]].merge(
        block_scores_df, on="geoid20", how="inner"
    )

    tp = _tmp_pop(merged)
    score_inputs = _build_score_inputs(merged, destinations, tp, config)
    overall_scores = _build_overall_scores(score_inputs, merged, segments_df, boundary_geom, config)
    mileage = _compute_mileage(segments_df)

    _logger.info(
        "neighborhood  output — overall_scores=%d  score_inputs=%d  mileage_rows=%d",
        len(overall_scores), len(score_inputs), len(mileage),
    )
    return {"overall_scores": overall_scores, "score_inputs": score_inputs, "mileage": mileage}


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

from bikescore.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd

    scores_dir = Path(input_paths["scores"])
    census_dir = Path(input_paths["census"])
    stress_dir = Path(input_paths["stress"])
    dest_dir = Path(input_paths["destinations"])
    conn_dir = Path(input_paths["connectivity"])

    block_scores_df = pd.read_parquet(scores_dir / "scores.parquet")
    blocks_gdf = gpd.read_parquet(census_dir / "census_blocks.parquet")
    segments_df = gpd.read_parquet(stress_dir / "stress.parquet")

    connectivity_df = pd.read_parquet(conn_dir / "connectivity.parquet")
    if not connectivity_df.empty and "high_stress" in connectivity_df.columns:
        reachable = (
            connectivity_df[connectivity_df["high_stress"]]
            .groupby("source_blockid20")
            .size()
            .rename("reachable_blocks")
        )
    else:
        reachable = pd.Series(dtype=int, name="reachable_blocks")
    blocks_gdf = blocks_gdf.copy()
    blocks_gdf["reachable_blocks"] = (
        blocks_gdf["geoid20"].astype(str).map(reachable).fillna(0).astype(int)
    )

    destinations: dict[str, pd.DataFrame] = {}
    for f in sorted(dest_dir.glob("dest_*.parquet")):
        name = f.stem[5:]
        destinations[name] = pd.read_parquet(f)

    if "length_m" not in segments_df.columns:
        segments_df = segments_df.copy()
        segments_df["length_m"] = segments_df.geometry.length

    result = compute_neighborhood(
        block_scores_df=block_scores_df,
        blocks_df=blocks_gdf,
        segments_df=segments_df,
        destinations=destinations,
        config=config,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result["overall_scores"].to_parquet(out / "neighborhood.parquet")
    result["score_inputs"].to_parquet(out / "score_inputs.parquet")
    if "mileage" in result:
        result["mileage"].to_parquet(out / "mileage.parquet")


NEIGHBORHOOD = StageSpec(
    name="neighborhood",
    depends_on=("scores", "census", "stress", "destinations", "connectivity"),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
