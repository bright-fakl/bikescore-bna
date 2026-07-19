"""Jobs stage: load LODES employment data for US cities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from bikescore.config import BNAConfig

STAGE_VERSION: str = "1.0.0"

_logger = logging.getLogger("bikescore")


def compute_jobs(
    lodes_main_path: str | Path | None,
    lodes_aux_path: str | Path | None = None,
) -> pd.DataFrame:
    """Load LODES WAC (Workplace Area Characteristics) data and return per-block job counts.

    Downloads and aggregates S000 (total jobs) from LODES OD main and aux tables.
    Returns empty DataFrame for non-US cities (pass None for both paths).

    Args:
        lodes_main_path: Path to state_od_main_jt00_{year}.csv (or None).
        lodes_aux_path: Path to state_od_aux_jt00_{year}.csv (or None).

    Returns:
        DataFrame with columns: blockid20 (str, 15-char), jobs (int).
        Empty if no LODES data provided.
    """
    if lodes_main_path is None and lodes_aux_path is None:
        _logger.info("jobs  no LODES paths provided — returning empty")
        return pd.DataFrame(columns=["blockid20", "jobs"])

    dfs = []
    for path in [lodes_main_path, lodes_aux_path]:
        if path is None:
            continue
        p = Path(path)
        if p.exists():
            dfs.append(
                pd.read_csv(p, usecols=["w_geocode", "S000"], dtype={"w_geocode": str})
            )

    if not dfs:
        _logger.info("jobs  LODES files not found on disk — returning empty")
        return pd.DataFrame(columns=["blockid20", "jobs"])

    lodes = pd.concat(dfs, ignore_index=True)
    _logger.info("jobs  loaded %d file(s)  lodes_rows=%d", len(dfs), len(lodes))

    jobs_by_block = lodes.groupby("w_geocode")["S000"].sum()

    result = (
        jobs_by_block[jobs_by_block > 0]
        .reset_index()
        .rename(columns={"w_geocode": "blockid20", "S000": "jobs"})
    )
    result["blockid20"] = result["blockid20"].str.zfill(15)
    result["jobs"] = result["jobs"].astype(int)
    result = result.reset_index(drop=True)
    _logger.info("jobs  output — blocks=%d  total_jobs=%d", len(result), int(result["jobs"].sum()))
    return result


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from bikescore.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    lodes_main = input_paths.get("dataset:lodes_main")
    lodes_aux = input_paths.get("dataset:lodes_aux")

    result = compute_jobs(lodes_main, lodes_aux)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.to_parquet(out / "jobs.parquet")


JOBS = StageSpec(
    name="jobs",
    depends_on=(),
    dataset_inputs=("lodes_main", "lodes_aux"),
    version=STAGE_VERSION,
    run=_run,
)
