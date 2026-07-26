"""Census stage: load US census blocks or generate synthetic blocks for non-US cities."""

from __future__ import annotations

import logging
import random
import string
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

STAGE_VERSION: str = "1.0.0"

_logger = logging.getLogger("bikescore-bna")

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore_bna.config import BNAConfig

_PSEUDO_MERCATOR = "EPSG:3857"
_BLOCKID_LEN = 15


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


# Census attrs carried on the excluded-block export layer (besides geometry/block_class).
_EXCLUDED_BLOCK_ATTRS = ("geoid20", "pop20", "aland20", "awater20")


def classify_census_blocks(
    blocks_gdf: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    block_boundary_overlap: float = 0.50,
    exclude_water_blocks: bool = True,
) -> gpd.GeoDataFrame:
    """Label every block ``water`` / ``outside`` / ``included`` (priority in that order).

    Same water (``aland20 == 0``) and boundary-overlap tests as
    :func:`filter_census_blocks`, but retains **all** rows with a ``block_class``
    column so the excluded blocks can be exported as a separate, inert layer. The
    ``included`` rows are exactly the set :func:`filter_census_blocks` keeps.

    Args:
        blocks_gdf: Census blocks GeoDataFrame (any CRS).
        boundary: Analysis boundary GeoDataFrame (any CRS).
        block_boundary_overlap: Minimum in-boundary area fraction for ``included``.
        exclude_water_blocks: When set, zero-land blocks classify as ``water``.

    Returns:
        Copy of *blocks_gdf* with an added ``block_class`` column.
    """
    from shapely.ops import unary_union

    result = blocks_gdf.copy().reset_index(drop=True)

    blocks_ea = result.to_crs(epsg=6933)
    boundary_ea = boundary.to_crs(epsg=6933)
    boundary_geom_ea = unary_union(boundary_ea.geometry)
    block_areas = blocks_ea.geometry.area
    intersect_areas = blocks_ea.geometry.intersection(boundary_geom_ea).area
    overlap = (intersect_areas / block_areas).to_numpy()

    if exclude_water_blocks and "aland20" in result.columns:
        is_water = (result["aland20"] == 0).to_numpy()
    else:
        is_water = np.zeros(len(result), dtype=bool)

    result["block_class"] = np.where(
        is_water,
        "water",
        np.where(overlap < block_boundary_overlap, "outside", "included"),
    )
    return result


def _read_blocks_file(census_path: Path) -> gpd.GeoDataFrame:
    """Read a census-blocks file (.parquet or shapefile) and lowercase its columns."""
    import geopandas as gpd

    p = Path(census_path)
    blocks_gdf = gpd.read_parquet(p) if p.suffix == ".parquet" else gpd.read_file(p)
    blocks_gdf.columns = [c.lower() for c in blocks_gdf.columns]
    return blocks_gdf


def load_census_blocks(
    census_path: Path | None,
    boundary: gpd.GeoDataFrame,
    config: BNAConfig,
) -> gpd.GeoDataFrame:
    """Load census blocks for a city.

    For US cities (census_path provided): loads shapefile or parquet, normalises
    column names to lowercase, and filters to blocks that are ≥50% inside the
    city boundary (mirrors brokenspoke-analyzer delete_block_outside_buffer).

    For non-US cities (census_path=None): generates a synthetic regular grid over
    the boundary with uniform population, matching brokenspoke-analyzer's
    create_synthetic_population() approach.

    Args:
        census_path: Path to census blocks file (.parquet or shapefile), or None.
        boundary: City boundary GeoDataFrame (EPSG:4326).
        config: Pipeline configuration (unused for US; exposes cell_size_m in future).

    Returns:
        GeoDataFrame with at minimum columns: geoid20 (str), pop20 (int), geometry.
    """
    if census_path is not None:
        blocks_gdf = _read_blocks_file(census_path)

        n_before = len(blocks_gdf)
        blocks_gdf = filter_census_blocks(
            blocks_gdf,
            boundary,
            block_boundary_overlap=config.block_boundary_overlap,
            exclude_water_blocks=config.exclude_water_blocks,
        )
        _logger.info(
            "census  loaded %d blocks → %d after area filter (≥50%% in boundary)",
            n_before,
            len(blocks_gdf),
        )
        return blocks_gdf

    _logger.info("census  no census file — generating synthetic blocks for non-US city")
    return _create_synthetic_census_blocks(boundary)


def load_census_blocks_with_excluded(
    census_path: Path | None,
    boundary: gpd.GeoDataFrame,
    config: BNAConfig,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame | None]:
    """Load census blocks, returning both the ``included`` set and the excluded blocks.

    ``included`` is byte-for-byte what :func:`load_census_blocks` returns (via the
    unchanged :func:`filter_census_blocks`), preserving default parity. ``excluded`` is
    ``None`` for the synthetic (non-US) path or when nothing is excluded; otherwise it
    carries geometry + ``block_class`` + the key census attrs.
    """
    if census_path is None:
        return load_census_blocks(census_path, boundary, config), None

    raw = _read_blocks_file(census_path)
    included = filter_census_blocks(
        raw,
        boundary,
        block_boundary_overlap=config.block_boundary_overlap,
        exclude_water_blocks=config.exclude_water_blocks,
    )
    _logger.info(
        "census  loaded %d blocks → %d after area filter (≥50%% in boundary)",
        len(raw),
        len(included),
    )
    classified = classify_census_blocks(
        raw,
        boundary,
        block_boundary_overlap=config.block_boundary_overlap,
        exclude_water_blocks=config.exclude_water_blocks,
    )
    excluded = classified[classified["block_class"] != "included"].reset_index(drop=True)
    if excluded.empty:
        return included, None

    keep_cols = [c for c in _EXCLUDED_BLOCK_ATTRS if c in excluded.columns]
    keep_cols += ["block_class", excluded.geometry.name]
    return included, excluded[keep_cols]


def _create_synthetic_census_blocks(
    boundary: gpd.GeoDataFrame,
    cell_size_m: int = 200,
    population_per_cell: int = 100,
) -> gpd.GeoDataFrame:
    """Generate synthetic census blocks by gridding the city boundary.

    Projects the boundary to pseudo-mercator, creates a regular grid at
    cell_size_m resolution, and keeps cells that intersect the boundary geometry.
    Each cell gets a uniform population and a random 15-character GEOID20.

    Args:
        boundary: City boundary GeoDataFrame (any CRS).
        cell_size_m: Grid cell side length in metres (default 200m).
        population_per_cell: Synthetic population assigned to every cell (default 100).

    Returns:
        GeoDataFrame with columns: geoid20 (str), pop20 (int), geometry (EPSG:4326).
    """
    import geopandas as gpd
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    mercator_area = boundary.to_crs(_PSEUDO_MERCATOR)
    boundary_geom = unary_union(mercator_area.geometry)

    xmin, ymin, xmax, ymax = mercator_area.total_bounds
    cols = np.arange(xmin, xmax + cell_size_m, cell_size_m)
    rows = np.arange(ymin, ymax + cell_size_m, cell_size_m)

    cells = []
    for col in cols[:-1]:
        for row in rows[:-1]:
            cell = Polygon([
                (col, row),
                (col + cell_size_m, row),
                (col + cell_size_m, row + cell_size_m),
                (col, row + cell_size_m),
            ])
            if cell.intersects(boundary_geom):
                cells.append(cell)

    _logger.info(
        "census  synthetic grid: %d cells (%dm x %dm)",
        len(cells),
        cell_size_m,
        cell_size_m,
    )

    blocks = gpd.GeoDataFrame(
        {
            "geometry": cells,
            "pop20": population_per_cell,
            "geoid20": [
                "".join(random.choices(string.ascii_lowercase, k=_BLOCKID_LEN))
                for _ in range(len(cells))
            ],
        },
        crs=_PSEUDO_MERCATOR,
    ).to_crs(boundary.crs)

    return blocks.reset_index(drop=True)


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from bikescore_bna.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import geopandas as gpd
    from shapely.geometry import box

    census_path_str = input_paths.get("dataset:census")
    census_path = Path(census_path_str) if census_path_str else None

    boundary_path = input_paths.get("dataset:analysis_boundary") or input_paths.get(
        "dataset:boundary"
    )
    if boundary_path:
        boundary = gpd.read_file(boundary_path).to_crs(epsg=4326)
    elif census_path is not None:
        # No boundary provided — derive permissive bbox from census data so
        # filter_census_blocks passes all blocks through (unit test path).
        if census_path.suffix == ".parquet":
            _tmp = gpd.read_parquet(census_path)
        else:
            _tmp = gpd.read_file(census_path)
        _tmp = _tmp.to_crs(epsg=4326)
        minx, miny, maxx, maxy = _tmp.total_bounds
        boundary = gpd.GeoDataFrame(geometry=[box(minx, miny, maxx, maxy)], crs="EPSG:4326")
    else:
        # Non-US synthetic path — use world bbox; _create_synthetic_census_blocks
        # will clip the result to this boundary anyway.
        boundary = gpd.GeoDataFrame(geometry=[box(-180, -90, 180, 90)], crs="EPSG:4326")

    blocks_gdf, excluded_gdf = load_census_blocks_with_excluded(census_path, boundary, config)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    blocks_gdf.to_parquet(out / "census_blocks.parquet")
    if excluded_gdf is not None and not excluded_gdf.empty:
        excluded_gdf.to_parquet(out / "excluded_census_blocks.parquet")


CENSUS = StageSpec(
    name="census",
    depends_on=(),
    dataset_inputs=("census", "analysis_boundary"),
    version=STAGE_VERSION,
    run=_run,
)
