"""BNA derived-field providers + field catalogs (design-review Appendix A.5/A.8).

The grammar/engine is domain-agnostic; *all* BNA knowledge that is not data lives
here as **pinned Python providers** plus the typed **field catalogs** that map each
derived field to the provider that computes it. The rules' referenced-field set
drives which providers run (see :func:`bikescore_bna.decision.catalog.run_decision`).

Notably, the ``intersection_context`` provider relocates the formerly hardcoded
cross-row intersection join: it is now invoked only because the intersection-stress
rules reference ``node_signalized_*`` / ``has_high_cross_*`` fields.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bikescore_bna.decision import FieldCatalog, ProviderContext, register_provider

# Eligible crossing FCs for intersection stress (SQL EXISTS CASE: never _link).
_CROSSING_ALL = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})
_CROSSING_NO_TERT = frozenset({"motorway", "trunk", "primary", "secondary"})
_INT_DEFAULTS: dict[str, int] = {
    "primary_speed": 40, "primary_lanes": 2,
    "secondary_speed": 40, "secondary_lanes": 2,
    "tertiary_speed": 30, "tertiary_lanes": 1,
}


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series(pd.NA, index=df.index, dtype=object)


# ── stress: intersection context (relocated cross-row join) ──────────────────

def _crossing_high_stress_vec(
    cross: pd.DataFrame, defaults: dict[str, int] | None = None
) -> pd.Series:
    """Whether each crossing-road row creates a high-stress intersection.

    Mirrors the EXISTS CASE in stress_lesser_ints.sql / stress_tertiary_ints.sql.
    Columns: cross_fc, cross_one_way, cross_ft_lanes, cross_tf_lanes, cross_speed,
    rrfb, island. ``defaults`` supplies the per-FC crossing speed/lane fallbacks
    (StressConfig.crossing_speed_defaults); falls back to the BNA built-ins.
    """
    d = defaults if defaults is not None else _INT_DEFAULTS
    result = pd.Series(False, index=cross.index)
    fc = cross["cross_fc"]
    rrfb = cross["rrfb"].fillna(False).astype(bool)
    island = cross["island"].fillna(False).astype(bool)

    result |= fc.isin({"motorway", "trunk"})

    for cross_fc_set, def_speed, def_lanes in [
        ({"primary"}, d["primary_speed"], d["primary_lanes"]),
        ({"secondary"}, d["secondary_speed"], d["secondary_lanes"]),
        ({"tertiary"}, d["tertiary_speed"], d["tertiary_lanes"]),
    ]:
        fc_mask = fc.isin(cross_fc_set)
        if not fc_mask.any():
            continue
        sub = cross[fc_mask]
        rrfb_s = rrfb[fc_mask]
        island_s = island[fc_mask]
        speed = sub["cross_speed"].fillna(def_speed)
        ft_lanes = sub["cross_ft_lanes"]
        tf_lanes = sub["cross_tf_lanes"]
        one_way = sub["cross_one_way"]
        is_one_way = one_way.notna()

        ow_mask = fc_mask & is_one_way
        if ow_mask.any():
            ow_speed = speed[is_one_way]
            ow_ft = ft_lanes[is_one_way]
            ow_tf = tf_lanes[is_one_way]
            ow_rrfb = rrfb_s[is_one_way]
            ow_lanes = ow_ft.where(ow_ft.notna(), ow_tf).fillna(def_lanes)
            ow_high = ow_lanes > 2
            ow_high |= ow_rrfb & (ow_lanes == 2) & (ow_speed > 40)
            ow_high |= ow_rrfb & (ow_lanes < 2) & (ow_speed > 35)
            ow_high |= ~ow_rrfb & (ow_lanes == 2) & (ow_speed > 30)
            ow_high |= ~ow_rrfb & (ow_lanes < 2) & (ow_speed > 30)
            result[ow_mask] = result[ow_mask] | ow_high.values

        tw_mask = fc_mask & ~is_one_way
        if tw_mask.any():
            tw_speed = speed[~is_one_way]
            tw_ft = ft_lanes[~is_one_way].fillna(def_lanes)
            tw_tf = tf_lanes[~is_one_way].fillna(def_lanes)
            tw_total = tw_ft + tw_tf
            tw_rrfb = rrfb_s[~is_one_way]
            tw_island = island_s[~is_one_way]
            tw_high = tw_total > 4
            rrfb_4 = tw_rrfb & (tw_total == 4)
            rrfb_lt4 = tw_rrfb & (tw_total < 4)
            tw_high |= rrfb_4 & (tw_speed > 40)
            tw_high |= rrfb_4 & (tw_speed > 30) & ~tw_island
            tw_high |= rrfb_lt4 & (tw_speed > 35) & ~tw_island
            no_rrfb_4 = ~tw_rrfb & (tw_total == 4)
            no_rrfb_lt4 = ~tw_rrfb & (tw_total < 4)
            tw_high |= no_rrfb_4 & (tw_speed > 30)
            tw_high |= no_rrfb_4 & (tw_speed == 30) & ~tw_island
            tw_high |= no_rrfb_lt4 & (tw_speed > 30) & ~tw_island
            result[tw_mask] = result[tw_mask] | tw_high.values

    return result


def _compute_crossing_booleans(
    df: pd.DataFrame,
    adj_fc: pd.Series,
    node_attrs: pd.DataFrame,
    needed: set[str],
    defaults: dict[str, int] | None = None,
) -> dict[str, pd.Series]:
    """has_high_cross_* booleans via a vectorized self-join (fixed mechanics)."""
    def _ensure(col: str) -> pd.Series:
        return df[col] if col in df.columns else pd.Series(np.nan, index=df.index)

    road_at_node = pd.DataFrame({
        "cross_road_id": df["road_id"].values,
        "cross_name": _ensure("name").values,
        "cross_fc": adj_fc.values,
        "cross_one_way": _ensure("one_way").values,
        "cross_ft_lanes": _ensure("ft_lanes").values,
        "cross_tf_lanes": _ensure("tf_lanes").values,
        "cross_speed": _ensure("speed_limit").values,
    }, index=df.index)

    road_at_start = road_at_node.copy()
    road_at_start["node_id"] = _ensure("start_node_id").values
    road_at_end = road_at_node.copy()
    road_at_end["node_id"] = _ensure("end_node_id").values
    road_at_node_full = pd.concat([road_at_start, road_at_end], ignore_index=True)
    road_at_node_full = road_at_node_full.drop_duplicates(subset=["cross_road_id", "node_id"])

    def _exists_mask(node_col: str, eligible_fcs: frozenset) -> pd.Series:
        crossing_eligible = road_at_node_full[road_at_node_full["cross_fc"].isin(eligible_fcs)]
        if crossing_eligible.empty:
            return pd.Series(False, index=df.index)
        subj = pd.DataFrame({
            "_subj_idx": df.index,
            "road_id": df["road_id"].values,
            "name": _ensure("name").values,
            "node_id": _ensure(node_col).values,
        }, index=df.index)
        crossing = subj.merge(crossing_eligible, on="node_id", how="inner")
        crossing = crossing[crossing["road_id"] != crossing["cross_road_id"]]
        if crossing.empty:
            return pd.Series(False, index=df.index)
        same_name = (
            crossing["name"].notna()
            & crossing["cross_name"].notna()
            & (crossing["name"] == crossing["cross_name"])
        )
        crossing = crossing[~same_name]
        if crossing.empty:
            return pd.Series(False, index=df.index)
        crossing = crossing.merge(
            node_attrs[["rrfb", "island"]], left_on="node_id", right_index=True, how="left"
        )
        crossing["rrfb"] = crossing["rrfb"].fillna(False)
        crossing["island"] = crossing["island"].fillna(False)
        crossing = crossing.copy()
        crossing["is_high"] = _crossing_high_stress_vec(crossing, defaults)
        high_idx = set(crossing.loc[crossing["is_high"], "_subj_idx"])
        result = pd.Series(False, index=df.index)
        if high_idx:
            result.loc[list(high_idx)] = True
        return result

    out: dict[str, pd.Series] = {}
    if "has_high_cross_ft" in needed:
        out["has_high_cross_ft"] = _exists_mask("end_node_id", _CROSSING_ALL)
    if "has_high_cross_no_tert_ft" in needed:
        out["has_high_cross_no_tert_ft"] = _exists_mask("end_node_id", _CROSSING_NO_TERT)
    if "has_high_cross_tf" in needed:
        out["has_high_cross_tf"] = _exists_mask("start_node_id", _CROSSING_ALL)
    if "has_high_cross_no_tert_tf" in needed:
        out["has_high_cross_no_tert_tf"] = _exists_mask("start_node_id", _CROSSING_NO_TERT)
    return out


_INT_NODE_MAP = {
    "node_signalized_ft": ("end_node_id", "signalized"),
    "node_stop_ft": ("end_node_id", "stops"),
    "node_rrfb_ft": ("end_node_id", "rrfb"),
    "node_island_ft": ("end_node_id", "island"),
    "node_signalized_tf": ("start_node_id", "signalized"),
    "node_stop_tf": ("start_node_id", "stops"),
    "node_rrfb_tf": ("start_node_id", "rrfb"),
    "node_island_tf": ("start_node_id", "island"),
}
_INT_CROSS_COLS = frozenset({
    "has_high_cross_ft", "has_high_cross_tf",
    "has_high_cross_no_tert_ft", "has_high_cross_no_tert_tf",
})


@register_provider(
    "intersection_context",
    list(_INT_NODE_MAP) + list(_INT_CROSS_COLS),
)
def _provider_intersection_context(ctx: ProviderContext) -> dict[str, pd.Series]:
    """Node-attribute lookups + crossing-road EXISTS booleans, computed only for
    the fields the intersection rules reference. ``node_attrs`` (indexed by node
    id, columns signalized/stops/rrfb/island) and ``adj_fc`` come from the stage."""
    df = ctx.frame
    needed = set(ctx.needed)
    node_attrs = ctx.extras["node_attrs"]
    adj_fc = df["adj_fc"] if "adj_fc" in df.columns else ctx.extras["adj_fc"]
    crossing_defaults = ctx.get("crossing_speed_defaults")
    out: dict[str, pd.Series] = {}
    if "start_node_id" not in df.columns or "end_node_id" not in df.columns:
        return out
    for col, (node_col, attr) in _INT_NODE_MAP.items():
        if col in needed:
            out[col] = df[node_col].map(node_attrs[attr]).fillna(False).astype(bool)
    if needed & _INT_CROSS_COLS:
        out.update(
            _compute_crossing_booleans(df, adj_fc, node_attrs, needed, crossing_defaults)
        )
    return out


# ── Field catalogs ────────────────────────────────────────────────────────────

def attributes_catalog() -> FieldCatalog:
    cat = FieldCatalog("attributes")
    cat.frame("highway", "str").frame("bicycle", "str").frame("footway", "str")
    cat.frame("tracktype", "str").frame("functional_class", "enum")
    # Phase 35n: access_ok / footway_wide / is_golf_path / cls_* are now persist:false
    # attributes (real columns), not provider-derived fields — no derived() entries.
    return cat


def intersection_catalog() -> FieldCatalog:
    cat = FieldCatalog("intersection")
    cat.frame("adj_fc", "enum")
    for fld in list(_INT_NODE_MAP) + list(_INT_CROSS_COLS):
        cat.derived(fld, "intersection_context")
    return cat


ATTRIBUTES_CATALOG = attributes_catalog()
INTERSECTION_CATALOG = intersection_catalog()
