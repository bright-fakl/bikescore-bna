"""Attributes stage: assign functional_class, bike_infra, one_way, and park.

SQL equivalents (in execution order):
  features/one_way.sql           — one_way_car (clip-gate built-in attribute)
  features/functional_class.sql  — functional_class from highway tag  [assign_functional_class()]
  features/bike_infra.sql        — ft/tf_bike_infra + bike one_way    (attributes-gate attribute)
  features/park.sql              — ft/tf_park from parking tags        (attributes-gate attribute)
  features/class_adjustments.sql — promotes residential/unclassified → tertiary

Output columns added to ways_df:
  functional_class, ft_bike_infra, tf_bike_infra, one_way, ft_park, tf_park, xwalk
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    from bikescore.config import BNAConfig

_logger = logging.getLogger("bikescore")

# The dotted config paths this stage resolves into rule variables (Phase 34i/35f).
# Ported from the old ``AttributesStage._RULE_VARIABLES`` class attribute; passed
# explicitly to ``build_rule_variables`` since there is no base class to hold it.
_RULE_VARIABLES: dict[str, str] = {
    "speed_bare_unit": "imputation.bare_speed_unit",
    # Consumed by the speed_parsed attribute's fallback passes (Phase 35f):
    # city/state residential defaults injected as $var:, NULL → rule skipped.
    "city_default_speed": "city.default_speed",
    "state_default_speed": "city.state_default_speed",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return column if present, else a NULL Series of same index."""
    if name in df.columns:
        return df[name]
    return pd.Series(pd.NA, index=df.index, dtype=object)


# ── Shadow computations (replicates known SQL bugs for validation) ─────────────

def _compute_park_sql_shadow(df: pd.DataFrame) -> pd.DataFrame:
    """Add ft_park_sql / tf_park_sql columns replicating the SQL park.sql bug.

    The SQL runs three unconditional UPDATEs (both → right → left). The right
    and left passes each overwrite every row's ft_park / tf_park respectively,
    so the :both result is always discarded. The SQL final state is:
      ft_park = CASE(right tags) ELSE NULL   — :both result completely lost
      tf_park = CASE(left tags) ELSE NULL    — :both result completely lost
    """
    pl_right = _col(df, "parking:lane:right")
    pl_left = _col(df, "parking:lane:left")
    p_right = _col(df, "parking:right")
    p_left = _col(df, "parking:left")
    p_right_r = _col(df, "parking:right:restriction")
    p_left_r = _col(df, "parking:left:restriction")

    _PRESENT = ["parallel", "paralell", "diagonal", "perpendicular"]
    _NONE = ["no_parking", "no_stopping"]

    def _pv(lane_col: pd.Series, side_col: pd.Series, restr_col: pd.Series) -> pd.Series:
        cond = [
            lane_col.isin(_PRESENT), lane_col.isin(_NONE),
            side_col == "lane", side_col == "no",
            restr_col.isin(["no_stopping", "no_parking"]),
        ]
        return pd.Series(
            np.select(cond, [1.0, 0.0, 1.0, 0.0, 0.0], default=np.nan),
            index=lane_col.index,
        )

    ft_sql = _pv(pl_right, p_right, p_right_r)
    tf_sql = _pv(pl_left, p_left, p_left_r)
    df["ft_park_sql"] = ft_sql.where(ft_sql.notna(), None)
    df["tf_park_sql"] = tf_sql.where(tf_sql.notna(), None)
    return df


def _compute_bike_infra_sql_shadow(df: pd.DataFrame) -> pd.DataFrame:
    """Add tf_bike_infra_sql column replicating the SQL bike_infra.sql dead-code bug.

    Inside WHEN one_way_car='ft' THEN CASE, two conditions check one_way_car='tf'
    (always false), so opposite_track on left/right for tf on ft one-way roads
    yields NULL instead of 'track'. Shadow column = tf_bike_infra but NULL for
    those rows.
    Requires one_way_car to be present in df (call before drop).
    """
    owc = _col(df, "one_way_car")
    cw_l = _col(df, "cycleway:left")
    cw_r = _col(df, "cycleway:right")

    dead_code = (owc == "ft") & ((cw_l == "opposite_track") | (cw_r == "opposite_track"))
    df["tf_bike_infra_sql"] = df["tf_bike_infra"].copy() if "tf_bike_infra" in df.columns else None
    if "tf_bike_infra" in df.columns:
        df.loc[dead_code, "tf_bike_infra_sql"] = None
    return df


# ── Bike-infra facility width default (relocated from impute, Phase 35f) ──────

def _fill_facility_width_defaults(df: pd.DataFrame, config: BNAConfig) -> pd.DataFrame:
    """Fill ft/tf_bike_infra_width with the default facility width where a bike
    facility is present but no width tag parsed.

    ft/tf_bike_infra_width may be produced by the bike_infra_width attribute (from
    cycleway:*:width tags). Where the column is absent but the paired bike_infra column
    exists, create it so stress rules can compare widths against the 4 ft threshold.
    Default = config.imputation.default_facility_width_ft (5 ft), matching
    :default_facility_width in stress_segments_higher_order.sql. Relocated verbatim from
    the retired impute stage (Phase 35f) — not one of the four migrated attribute columns.
    """
    for _w_col, _infra_col in (
        ("ft_bike_infra_width", "ft_bike_infra"),
        ("tf_bike_infra_width", "tf_bike_infra"),
    ):
        if _w_col not in df.columns and _infra_col in df.columns:
            df[_w_col] = pd.array([None] * len(df), dtype=pd.Float64Dtype())
        if _w_col in df.columns:
            _null_before = int(df[_w_col].isna().sum())
            _facility_w = config.imputation.default_facility_width_ft
            df[_w_col] = df[_w_col].fillna(_facility_w)
            _filled = _null_before - int(df[_w_col].isna().sum())
            if _filled:
                _logger.info("attributes  %s: %d filled with default %g ft",
                             _w_col, _filled, _facility_w)
    return df


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from bikescore.stage import (  # noqa: E402
    StageSpec,
    apply_attribute_fallbacks,
    apply_attributes,
    build_rule_variables,
)


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd

    parse_dir = Path(input_paths["parse"])
    ways_df = gpd.read_parquet(parse_dir / "ways_raw.parquet")

    variables = {**config.variables, **build_rule_variables(_RULE_VARIABLES, config)}
    n_in = len(ways_df)
    _logger.info("attributes  input_ways=%d", n_in)

    # Observed phase (Phase 35n): every attribute in topo order. functional_class —
    # and the access_ok/footway_wide/is_golf_path/cls_* flag attributes it depends on —
    # are ordinary attributes now; functional_class lands last via its `after:` deps,
    # reading the flags' observed (pre-imputation) outputs exactly as the old
    # run_pass(class_promotion) did. No providers; no special stage steps.
    result_df = apply_attributes(ways_df, config.attributes, vars=variables)

    # Drop rows with NULL functional_class (no valid highway type — matches SQL DELETE).
    # Phase 35n moves this from "before the other attributes" to "after the observed
    # phase"; the other attributes are row-wise, so surviving-row values are unchanged.
    if "functional_class" in result_df.columns:
        result_df = result_df[result_df["functional_class"].notna()].copy()
    _logger.info("attributes  functional_class — kept=%d  dropped=%d (no valid functional class)",
                 len(result_df), n_in - len(result_df))

    # Drop non-persisted scratch columns (the Phase 35n flag attributes): computed for
    # functional_class to consume, never written to the stage output. Replaces the
    # ad-hoc df.drop(columns=[access_ok, …]) / [cls_*] cleanups.
    if config.attributes is not None:
        _scratch = [c for c in config.attributes.non_persisted_columns() if c in result_df.columns]
        if _scratch:
            result_df = result_df.drop(columns=_scratch)

    # Imputation as attribute fallback passes (Phase 35f): the FC/config-default tail of
    # speed_limit / ft_lanes / tf_lanes runs AFTER the observed phase so the promoted
    # functional_class drives the defaults, with auto {col}_imputed flags.
    result_df = apply_attribute_fallbacks(result_df, config.attributes, vars=variables)

    # Bike-infra facility-width default (relocated from the retired impute stage).
    result_df = _fill_facility_width_defaults(result_df, config)

    # Shadow columns for SQL-bug-parity validation; one_way_car still present
    result_df = _compute_bike_infra_sql_shadow(result_df)
    result_df = _compute_park_sql_shadow(result_df)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result_df.to_parquet(out / "ways_classified.parquet")


ATTRIBUTES = StageSpec(
    name="attributes",
    depends_on=("parse",),
    dataset_inputs=(),
    version=STAGE_VERSION,
    run=_run,
)
