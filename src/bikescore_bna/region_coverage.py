"""Region coverage detector + US state crosswalk (Phase 2, spec §9).

Extent-expanding boundary work — a non-zero ``network_buffer_m``, ``convex_hull``, or an
``override_geometry`` source — can push the analysis + clip extent past the regional data
already downloaded for the city's home state. Multi-region acquisition (``extra_regions``)
supplies the neighbours; this module is the **guard** that makes a silent spill fail
loudly instead of clipping against a PBF missing those roads.

It owns three things:

- the **US state crosswalk** (slug ↔ FIPS ↔ abbr), keyed off the existing
  ``_US_STATE_SLUGS`` / ``_FIPS_TO_ABBR`` tables in :mod:`bikescore_bna.acquire` plus the
  one new ``slug → FIPS`` table here;
- fetching + caching the Geofabrik ``index-v1.json`` (per-extract polygons), the single
  region-polygon source — US Geofabrik extracts follow state lines, so a touched extract
  ↔ a touched state is exact and one computation drives OSM, census and LODES;
- the **advisory** coverage check: intersect the (buffered) extent with the acquired
  region polygons and raise an actionable error naming any region needed but not acquired.

The check is US-scoped — US state extracts are identified by their ``id`` shape
``us/<slug>`` (their ``parent`` is ``north-america``, not ``us``). Non-US census/LODES are
synthetic and never expand; non-US OSM multi-region relies on explicit ``extra_regions``
without the geometric assertion.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    import geopandas as gpd
    from shapely.geometry.base import BaseGeometry

    from bikescore_bna.city import CityIdentity

_logger = logging.getLogger("bikescore-bna")

_WGS84 = 4326
_GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"
_USER_AGENT = "bikescore-bna/0.1 (https://github.com/PeopleForBikes/bna-core)"

# Fraction of the extent that may fall outside the acquired regions before the coverage
# check trips — absorbs the tiny border slivers Geofabrik extracts overlap by.
_COVERAGE_AREA_EPS = 0.005

# Geofabrik state slug → US state FIPS. The one crosswalk table Phase 2 adds; slug→abbr
# and abbr→FIPS are then reachable via acquire's ``_FIPS_TO_ABBR``.
_US_STATE_SLUG_TO_FIPS: dict[str, str] = {
    "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05",
    "california": "06", "colorado": "08", "connecticut": "09", "delaware": "10",
    "district-of-columbia": "11", "florida": "12", "georgia": "13", "hawaii": "15",
    "idaho": "16", "illinois": "17", "indiana": "18", "iowa": "19", "kansas": "20",
    "kentucky": "21", "louisiana": "22", "maine": "23", "maryland": "24",
    "massachusetts": "25", "michigan": "26", "minnesota": "27", "mississippi": "28",
    "missouri": "29", "montana": "30", "nebraska": "31", "nevada": "32",
    "new-hampshire": "33", "new-jersey": "34", "new-mexico": "35", "new-york": "36",
    "north-carolina": "37", "north-dakota": "38", "ohio": "39", "oklahoma": "40",
    "oregon": "41", "pennsylvania": "42", "rhode-island": "44", "south-carolina": "45",
    "south-dakota": "46", "tennessee": "47", "texas": "48", "utah": "49",
    "vermont": "50", "virginia": "51", "washington": "53", "west-virginia": "54",
    "wisconsin": "55", "wyoming": "56", "puerto-rico": "72",
}


class RegionCoverageError(RuntimeError):
    """Raised when the analysis/clip extent needs regions that are not being acquired.

    Advisory (spec §9): it never triggers a download — it tells the caller which regions
    to add to ``extra_regions``.
    """


# ── US state crosswalk ────────────────────────────────────────────────────────


def normalize_region(region: str) -> str:
    """Normalize a region name or slug to its Geofabrik slug (lowercase, hyphenated).

    Accepts a US state *name* (``"district of columbia"``) or an already-normalized
    *slug* (``"district-of-columbia"``); non-US inputs are passed through as slugs.
    """
    from bikescore_bna.acquire import _US_STATE_SLUGS

    r = region.strip().lower()
    if r in _US_STATE_SLUGS:
        return _US_STATE_SLUGS[r]
    return r.replace(" ", "-")


def slug_to_fips(slug: str) -> str | None:
    """US state FIPS for a Geofabrik state slug, or ``None`` if it is not a US state."""
    return _US_STATE_SLUG_TO_FIPS.get(slug)


def slug_to_abbr(slug: str) -> str | None:
    """US state USPS abbreviation for a Geofabrik state slug, or ``None``."""
    from bikescore_bna.acquire import _FIPS_TO_ABBR

    fips = slug_to_fips(slug)
    return _FIPS_TO_ABBR.get(fips) if fips is not None else None


def resolve_acquire_regions(city: CityIdentity, extra_regions: list[str]) -> list[str]:
    """Ordered, de-duplicated list of region slugs to acquire: home region first.

    The home region is the city's state (``city.region``); ``extra_regions`` follow,
    normalized to slugs. Duplicates (e.g. a home state also listed in ``extra_regions``)
    collapse to one.
    """
    slugs: list[str] = []
    if city.region:
        slugs.append(normalize_region(city.region))
    for r in extra_regions:
        slugs.append(normalize_region(r))
    seen: set[str] = set()
    ordered: list[str] = []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


# ── Geofabrik index (region polygons) ─────────────────────────────────────────


def _index_cache_path(cache_dir: Path | None) -> Path:
    base = cache_dir if cache_dir is not None else _default_index_cache_dir()
    return Path(base) / "geofabrik-index-v1.json"


def _default_index_cache_dir() -> Path:
    env = os.environ.get("BIKESCORE_PBF_CACHE")
    return Path(env).expanduser() if env else Path.home() / ".bikescore-bna" / "pbf"


def load_geofabrik_index(
    cache_dir: Path | None = None, *, force: bool = False
) -> gpd.GeoDataFrame:
    """Fetch (and cache) the Geofabrik ``index-v1.json`` as a GeoDataFrame.

    Columns of interest: ``id`` (extract id, e.g. ``"us/maryland"``), ``parent``
    (``"us"`` for US states), and ``geometry`` (the extract polygon, WGS84). The JSON is
    cached under *cache_dir* (default: the shared PBF cache) and reused unless *force*.
    """
    import geopandas as gpd

    path = _index_cache_path(cache_dir)
    if force or not path.exists():
        _logger.info("acquire  fetching Geofabrik index: %s", _GEOFABRIK_INDEX_URL)
        resp = requests.get(
            _GEOFABRIK_INDEX_URL, headers={"User-Agent": _USER_AGENT}, timeout=120
        )
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)

    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=_WGS84)
    return gdf


# ── Coverage extent + check ────────────────────────────────────────────────────


def coverage_extent(analysis_gdf: gpd.GeoDataFrame, network_buffer_m: float) -> BaseGeometry:
    """The clip/coverage extent: the analysis boundary ⊕ ``network_buffer_m`` (WGS84).

    Buffering runs in the boundary's UTM estimate so the margin is a true metric length,
    matching how ``parse.pre_clip_pbf`` buffers the clip — so the checked extent equals
    the clipped extent.
    """
    import geopandas as gpd
    from shapely.ops import unary_union

    geom = unary_union(analysis_gdf.geometry)
    if not network_buffer_m:
        return geom
    gs = gpd.GeoSeries([geom], crs=f"EPSG:{_WGS84}")
    utm = gs.estimate_utm_crs()
    return gs.to_crs(utm).buffer(network_buffer_m).to_crs(epsg=_WGS84).iloc[0]


def _us_state_extracts(idx: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """The US state extract rows of a Geofabrik index, tagged with a ``slug`` column.

    US state extracts are identified by their ``id`` shape ``us/<slug>`` (e.g.
    ``us/maryland``) — *not* by ``parent``, which is ``north-america`` for them. Grouping
    extracts (``us-midwest`` …) and the nested-id case are excluded by requiring exactly
    one path segment after ``us/``.
    """
    if "id" not in idx.columns:
        return idx.iloc[0:0].assign(slug=[])
    ids = idx["id"].astype(str)
    us = idx[ids.str.match(r"^us/[^/]+$")].copy()
    us["slug"] = us["id"].astype(str).str.rsplit("/", n=1).str[-1]
    return us


def missing_regions(
    analysis_gdf: gpd.GeoDataFrame,
    network_buffer_m: float,
    acquired_slugs: list[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """US state slugs the (buffered) extent needs but *acquired_slugs* does not cover.

    Authoritative for both the §9 gate and the ``--dry-run`` report. **Difference**-based,
    not raw overlap: ``uncovered = extent − ⋃(acquired extract polygons)``, then the
    returned regions are the extracts covering a non-trivial part of that gap. Because
    Geofabrik extracts overlap slightly at borders, an acquired region's own extract
    already covers a border city's exact boundary — so a border city with no buffer yields
    ``[]`` (nothing extra needed), while a buffer that pushes past the home extract yields
    the neighbours it spills into. Empty ⇒ the acquired set covers the extent. May raise if
    the Geofabrik index cannot be fetched.
    """
    from shapely.ops import unary_union

    us = _us_state_extracts(load_geofabrik_index(cache_dir))
    if us.empty:
        return []

    extent = coverage_extent(analysis_gdf, network_buffer_m)
    extent_area = extent.area
    if extent_area <= 0:
        return []

    acquired = us[us["slug"].isin(acquired_slugs)]
    acquired_geom = unary_union(acquired.geometry) if not acquired.empty else None
    uncovered = extent if acquired_geom is None else extent.difference(acquired_geom)

    if uncovered.is_empty or uncovered.area <= _COVERAGE_AREA_EPS * extent_area:
        return []

    gap_area = uncovered.area
    out = [
        slug
        for slug, geom in zip(us["slug"], us.geometry, strict=True)
        if slug not in acquired_slugs
        and geom.intersects(uncovered)
        and geom.intersection(uncovered).area > _COVERAGE_AREA_EPS * gap_area
    ]
    return sorted(set(out))


def check_coverage(
    analysis_gdf: gpd.GeoDataFrame,
    network_buffer_m: float,
    acquired_slugs: list[str],
    *,
    cache_dir: Path | None = None,
) -> None:
    """Advisory §9 gate: raise if *acquired_slugs* does not cover the (buffered) extent.

    Thin wrapper over :func:`missing_regions` that raises :class:`RegionCoverageError`
    naming the regions to add to ``extra_regions``. Best-effort: if the Geofabrik index
    cannot be fetched (offline), logs a warning and returns — the check is a guard, not a
    hard dependency of acquire.
    """
    try:
        missing = missing_regions(
            analysis_gdf, network_buffer_m, acquired_slugs, cache_dir=cache_dir
        )
    except Exception as exc:  # advisory guard degrades gracefully when offline
        _logger.warning("acquire  coverage check skipped (Geofabrik index unavailable): %s", exc)
        return

    if not missing:
        return

    raise RegionCoverageError(
        "The analysis boundary"
        + (f" buffered by {network_buffer_m:g} m" if network_buffer_m else "")
        + " extends into regions that are not being acquired: "
        + f"{missing}. Add them to extra_regions (e.g. extra_regions={missing}) "
        "or reduce boundary.network_buffer_m / the expanding transform."
    )
