"""Data acquisition — the database-free input path for one city.

Downloads (and caches) the five raw inputs ``score_city`` needs: the OSM PBF (clipped
to the city boundary), the city boundary GeoJSON, and — for US cities — 2020 census
blocks and LODES employment CSVs. This is the DB-free carve-out of bna-core's acquire:
no SQLite ``Dataset``/``DataFile`` registration (that is the app's ``acquire_service``),
just files written to an output directory under content-addressed names.

The public entry point is :func:`acquire_city`, a thin wrapper over the default
:class:`InputProvider`. The provider seam (index §8/§9.1) is where other geographies
plug in; the US census/LODES path is the default and only shipped provider.

US-centric: census + LODES are US-only. Non-US cities get a Nominatim boundary and no
jobs data. Regional PBFs are shared across cities in ``~/.bikescore-bna/pbf/`` so a second
city in the same state reuses the download.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

import requests

from bikescore_bna.data_pool import store_file

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore_bna.city import CityIdentity

_logger = logging.getLogger("bikescore-bna")

_USER_AGENT = "bikescore-bna/0.1 (https://github.com/PeopleForBikes/bna-core)"
_NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
_GEOFABRIK_BASE = "https://download.geofabrik.de"
_LODES_BASE = "https://lehd.ces.census.gov/data/lodes/LODES8"

_PYGRIS_YEAR = 2024
_CENSUS_BLOCKS_YEAR = 2020

# Country name (lowercase) → Geofabrik URL prefix (relative to base URL)
_COUNTRY_PREFIX: dict[str, str] = {
    "austria": "europe/", "belgium": "europe/", "croatia": "europe/",
    "czech republic": "europe/", "denmark": "europe/", "finland": "europe/",
    "france": "europe/", "germany": "europe/", "great britain": "europe/",
    "united kingdom": "europe/", "greece": "europe/", "hungary": "europe/",
    "ireland": "europe/", "italy": "europe/", "luxembourg": "europe/",
    "netherlands": "europe/", "norway": "europe/", "poland": "europe/",
    "portugal": "europe/", "romania": "europe/", "russia": "",
    "spain": "europe/", "sweden": "europe/", "switzerland": "europe/",
    "canada": "north-america/", "mexico": "north-america/",
    "brazil": "south-america/", "argentina": "south-america/",
    "chile": "south-america/", "colombia": "south-america/",
    "egypt": "africa/", "kenya": "africa/", "nigeria": "africa/",
    "south africa": "africa/",
    "australia": "australia-oceania/", "new zealand": "australia-oceania/",
}

# Countries whose Geofabrik filename slug differs from their name
_COUNTRY_SLUG_OVERRIDE: dict[str, str] = {
    "great britain": "great-britain", "united kingdom": "great-britain",
    "south korea": "south-korea", "czech republic": "czech-republic",
    "south africa": "south-africa", "new zealand": "new-zealand",
}

# US state name (lowercase) → Geofabrik slug
_US_STATE_SLUGS: dict[str, str] = {
    "alabama": "alabama", "alaska": "alaska", "arizona": "arizona",
    "arkansas": "arkansas", "california": "california", "colorado": "colorado",
    "connecticut": "connecticut", "delaware": "delaware",
    "district of columbia": "district-of-columbia", "florida": "florida",
    "georgia": "georgia", "hawaii": "hawaii", "idaho": "idaho",
    "illinois": "illinois", "indiana": "indiana", "iowa": "iowa",
    "kansas": "kansas", "kentucky": "kentucky", "louisiana": "louisiana",
    "maine": "maine", "maryland": "maryland", "massachusetts": "massachusetts",
    "michigan": "michigan", "minnesota": "minnesota", "mississippi": "mississippi",
    "missouri": "missouri", "montana": "montana", "nebraska": "nebraska",
    "nevada": "nevada", "new hampshire": "new-hampshire", "new jersey": "new-jersey",
    "new mexico": "new-mexico", "new york": "new-york", "north carolina": "north-carolina",
    "north dakota": "north-dakota", "ohio": "ohio", "oklahoma": "oklahoma",
    "oregon": "oregon", "pennsylvania": "pennsylvania", "rhode island": "rhode-island",
    "south carolina": "south-carolina", "south dakota": "south-dakota",
    "tennessee": "tennessee", "texas": "texas", "utah": "utah", "vermont": "vermont",
    "virginia": "virginia", "washington": "washington", "west virginia": "west-virginia",
    "wisconsin": "wisconsin", "wyoming": "wyoming", "puerto rico": "puerto-rico",
}

# US state FIPS → USPS abbreviation (lowercase), for LODES URLs
_FIPS_TO_ABBR: dict[str, str] = {
    "01": "al", "02": "ak", "04": "az", "05": "ar", "06": "ca",
    "08": "co", "09": "ct", "10": "de", "11": "dc", "12": "fl",
    "13": "ga", "15": "hi", "16": "id", "17": "il", "18": "in",
    "19": "ia", "20": "ks", "21": "ky", "22": "la", "23": "me",
    "24": "md", "25": "ma", "26": "mi", "27": "mn", "28": "ms",
    "29": "mo", "30": "mt", "31": "ne", "32": "nv", "33": "nh",
    "34": "nj", "35": "nm", "36": "ny", "37": "nc", "38": "nd",
    "39": "oh", "40": "ok", "41": "or", "42": "pa", "44": "ri",
    "45": "sc", "46": "sd", "47": "tn", "48": "tx", "49": "ut",
    "50": "vt", "51": "va", "53": "wa", "54": "wv", "55": "wi",
    "56": "wy", "72": "pr",
}


class PbfMeta(NamedTuple):
    """Metadata for a downloaded regional PBF (URL, timing, size, checksum)."""

    url: str
    downloaded_at: str
    size_bytes: int
    sha256: str
    cached_filename: str = ""


_PBF_CACHE_ENV = "BIKESCORE_PBF_CACHE"


def _default_pbf_cache_dir() -> Path:
    """Default shared regional-PBF cache: ``$BIKESCORE_PBF_CACHE`` or ``~/.bikescore-bna/pbf``.

    The core resolves this itself and never reads the orchestration layer's global
    settings file. Callers relocate the cache by passing ``pbf_cache_dir=`` (to
    :func:`acquire_city`) or setting the env var.
    """
    env = os.environ.get(_PBF_CACHE_ENV)
    return Path(env).expanduser() if env else Path.home() / ".bikescore-bna" / "pbf"


@dataclass
class AcquireConfig:
    """Where acquisition reads/writes: the per-city output pool and the shared PBF cache."""

    project_data_dir: Path = field(default_factory=lambda: Path("./data"))
    pbf_cache_dir: Path = field(default_factory=_default_pbf_cache_dir)
    force_download: bool = False


# ── Boundary ─────────────────────────────────────────────────────────────────

def _fetch_boundary_census(city: CityIdentity, tmp_dir: Path) -> Path:
    """Fetch a US city boundary from the Census Bureau via pygris. Returns a GeoJSON path.

    Mirrors brokenspoke-analyzer's ``retrieve_city_boundaries``: tries ``places()``
    first, falls back to ``county_subdivisions()`` if the place FIPS is not found.
    """
    import pygris

    fips = city.fips_code
    assert fips is not None
    state_fips = fips[:2]
    place_fips = fips[2:]

    _logger.info("acquire  fetching boundary from Census Bureau (FIPS %s)", fips)
    places = pygris.places(state=state_fips, cache=True, year=_PYGRIS_YEAR)
    city_gdf = places[places["PLACEFP"] == place_fips]

    if city_gdf.empty:
        _logger.debug("acquire  FIPS %s not in places, trying county_subdivisions", fips)
        subs = pygris.county_subdivisions(state=state_fips, cache=True, year=_PYGRIS_YEAR)
        city_gdf = subs[subs["COUSUBFP"] == place_fips]

    if city_gdf.empty:
        raise ValueError(
            f"Census Bureau has no boundary for FIPS {fips!r} ({city.name}). "
            "Check that city.fips_code is correct."
        )

    if city_gdf.crs is None or city_gdf.crs.to_epsg() != 4326:
        city_gdf = city_gdf.to_crs(epsg=4326)

    out = tmp_dir / "boundary.geojson"
    city_gdf.to_file(out, driver="GeoJSON")
    return out


def _fetch_boundary_nominatim(city: CityIdentity, tmp_dir: Path) -> Path:
    """Fetch a non-US city boundary polygon from Nominatim. Returns a GeoJSON path."""
    query = city.name
    if city.region:
        query = f"{city.name}, {city.region}"
    if city.country:
        query = f"{query}, {city.country}"

    _logger.info("acquire  fetching boundary from Nominatim: %s", query)
    resp = requests.get(
        _NOMINATIM_SEARCH,
        params={
            "q": query, "polygon_geojson": 1, "format": "json",
            "addressdetails": 0, "limit": 5,
        },
        headers={"User-Agent": _USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    time.sleep(1.0)  # Nominatim rate limit: 1 req/s

    if not results:
        raise ValueError(f"Nominatim returned no results for {query!r}.")

    admin = [r for r in results if r.get("type") == "administrative"] or results
    best = max(admin, key=lambda r: float(r.get("importance", 0)))
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": best["geojson"],
            "properties": {
                "display_name": best.get("display_name", ""),
                "osm_id": best.get("osm_id"),
                "place_id": best.get("place_id"),
            },
        }],
    }
    out = tmp_dir / "boundary.geojson"
    with open(out, "w") as f:
        json.dump(geojson, f)
    return out


def _fetch_boundary_tmp(city: CityIdentity, tmp_dir: Path) -> Path:
    """Fetch the city boundary (Census for US, Nominatim otherwise) into *tmp_dir*."""
    if city.fips_code is not None:
        return _fetch_boundary_census(city, tmp_dir)
    return _fetch_boundary_nominatim(city, tmp_dir)


# ── Regional OSM PBF (shared cache) ──────────────────────────────────────────

def _geofabrik_url_for(country: str, region: str | None, base_url: str) -> str:
    """Build the Geofabrik state/country PBF URL from raw country/region strings."""
    country = country.lower()
    if country in ("united states", "us", "usa"):
        region = (region or "").lower()
        slug = _US_STATE_SLUGS.get(region, region.replace(" ", "-"))
        return f"{base_url}/north-america/us/{slug}-latest.osm.pbf"
    prefix = _COUNTRY_PREFIX.get(country, "")
    slug = _COUNTRY_SLUG_OVERRIDE.get(country, country.replace(" ", "-"))
    return f"{base_url}/{prefix}{slug}-latest.osm.pbf"


def _build_geofabrik_url(city: CityIdentity) -> str:
    return _geofabrik_url_for(city.country, city.region, _GEOFABRIK_BASE)


def _pbf_rel_path_from_url(url: str) -> str:
    """Extract the domain-relative path from a Geofabrik URL."""
    prefix = _GEOFABRIK_BASE + "/"
    if url.startswith(prefix):
        return url[len(prefix):]
    parts = url.split("/", 3)
    return parts[-1] if len(parts) >= 4 else url.rsplit("/", 1)[-1]


def _find_pbf_by_url(cache_dir: Path, url: str) -> tuple[Path, PbfMeta] | None:
    """Scan *cache_dir* for a cached ``.osm.pbf`` whose sidecar records *url*."""
    if not cache_dir.exists():
        return None
    candidates: list[tuple[Path, dict]] = []
    for pbf in cache_dir.glob("*.osm.pbf"):
        sidecar = Path(str(pbf) + ".meta.json")
        if not sidecar.exists():
            continue
        try:
            data = json.loads(sidecar.read_text())
        except Exception:
            continue
        if data.get("url") == url:
            candidates.append((pbf, data))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1].get("downloaded_at", ""), reverse=True)
    pbf, data = candidates[0]
    return pbf, PbfMeta(
        url=data["url"], downloaded_at=data["downloaded_at"],
        size_bytes=data["size_bytes"], sha256=data["sha256"], cached_filename=pbf.name,
    )


def _download_state_pbf(city: CityIdentity, config: AcquireConfig) -> tuple[Path, PbfMeta]:
    """Download (or reuse cached) the regional PBF for *city*. Returns (path, meta)."""
    url = _build_geofabrik_url(city)
    rel_path = _pbf_rel_path_from_url(url)
    cache_dir = config.pbf_cache_dir / Path(rel_path).parent

    if not config.force_download:
        hit = _find_pbf_by_url(cache_dir, url)
        if hit is not None:
            path, meta = hit
            _logger.info("acquire  regional PBF cache hit: %s", path.name)
            return path, meta

    _logger.info("acquire  downloading regional PBF: %s", url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    tmp_path = cache_dir / "download.pbf.tmp"
    size_bytes = 0
    downloaded_at = datetime.now(UTC).isoformat()

    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, stream=True, timeout=600)
    resp.raise_for_status()
    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
            h.update(chunk)
            size_bytes += len(chunk)

    sha256 = h.hexdigest()
    url_slug = Path(rel_path).name.replace(".osm.pbf", "").removesuffix("-latest")
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    fname = f"{url_slug}-{date_str}-{sha256[:12]}.osm.pbf"
    cache_path = cache_dir / fname
    tmp_path.replace(cache_path)
    _logger.info("acquire  regional PBF saved: %s (%.0f MB)", fname, size_bytes / 1e6)

    meta = PbfMeta(
        url=url, downloaded_at=downloaded_at, size_bytes=size_bytes,
        sha256=sha256, cached_filename=fname,
    )
    sidecar = Path(str(cache_path) + ".meta.json")
    tmp_sidecar = Path(str(sidecar) + ".tmp")
    tmp_sidecar.write_text(json.dumps({
        "url": url, "downloaded_at": downloaded_at,
        "size_bytes": size_bytes, "sha256": sha256,
    }, indent=2))
    tmp_sidecar.replace(sidecar)
    return cache_path, meta


# ── Census blocks (US only) ──────────────────────────────────────────────────

def _download_census_blocks_tmp(
    state_fips: str, boundary_gdf: gpd.GeoDataFrame, tmp_dir: Path,
) -> Path | None:
    """Download 2020 census blocks for the state, filter to the boundary. Returns a path."""
    import pygris

    _logger.info("acquire  downloading census blocks (state=%s)", state_fips)
    try:
        blocks = pygris.blocks(state=state_fips, year=_CENSUS_BLOCKS_YEAR, cache=True)
        blocks.columns = [c.lower() for c in blocks.columns]
        boundary_union = boundary_gdf.geometry.union_all()
        blocks = blocks[blocks.geometry.intersects(boundary_union)].copy()
        blocks = blocks.reset_index(drop=True)
        _logger.info("acquire  %d census blocks within city boundary", len(blocks))
        out = tmp_dir / "census.parquet"
        blocks.to_parquet(out)
        return out
    except Exception as exc:
        _logger.warning("acquire  census block download failed: %s", exc)
        return None


# ── LODES employment (US only) ───────────────────────────────────────────────

def _state_abbr_from_fips(state_fips: str) -> str | None:
    return _FIPS_TO_ABBR.get(state_fips.zfill(2))


def _lodes_latest_year(state_abbr: str, base_url: str) -> int:
    """Probe for the most recent available LODES year for *state_abbr*."""
    import datetime

    current_year = datetime.date.today().year
    for year in range(current_year, 2001, -1):
        url = f"{base_url}/{state_abbr.lower()}/od/{state_abbr.lower()}_od_main_JT00_{year}.csv.gz"
        try:
            resp = requests.head(url, headers={"User-Agent": _USER_AGENT}, timeout=10)
            time.sleep(0.5)
            if resp.status_code == 200:
                return year
        except requests.RequestException:
            continue
    return 2020


def _download_lodes_tmp(
    state_abbr: str, year: int, tmp_dir: Path,
) -> tuple[Path | None, Path | None]:
    """Download LODES main+aux OD CSVs for *state_abbr*/*year*. Returns (main, aux)."""
    state = state_abbr.lower()
    _logger.info("acquire  downloading LODES %d for %s", year, state.upper())
    main_url = f"{_LODES_BASE}/{state}/od/{state}_od_main_JT00_{year}.csv.gz"
    aux_url = f"{_LODES_BASE}/{state}/od/{state}_od_aux_JT00_{year}.csv.gz"
    main = _download_lodes_file_tmp(main_url, tmp_dir / "lodes_main.csv")
    aux = _download_lodes_file_tmp(aux_url, tmp_dir / "lodes_aux.csv")
    return main, aux


def _download_lodes_file_tmp(url: str, dest_csv: Path) -> Path | None:
    gz_path = dest_csv.with_suffix(".csv.gz")
    try:
        _download_file(url, gz_path)
        _gunzip(gz_path, dest_csv)
        gz_path.unlink(missing_ok=True)
        return dest_csv
    except requests.HTTPError as e:
        _logger.warning("acquire  LODES file not available: %s (%s)", url, e)
        gz_path.unlink(missing_ok=True)
        return None


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, stream=True, timeout=300)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)


def _gunzip(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as f_in, open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


# ── Public entry point + provider seam ───────────────────────────────────────

@runtime_checkable
class InputProvider(Protocol):
    """The seam that produces the raw ``dataset_inputs`` ``score_city`` consumes.

    A provider maps a :class:`~bikescore_bna.city.CityIdentity` to the five named input
    files. The US census/LODES provider is the default; another geography plugs in by
    implementing this protocol (index §8/§9.1). The orchestration app treats the
    dataset names as opaque — the provider is what gives them meaning.
    """

    def acquire(
        self, city: CityIdentity, out_dir: Path, *, force: bool = False,
    ) -> dict[str, Path]:
        """Produce ``{"osm", "boundary", "census", "lodes_main", "lodes_aux"}`` -> path.

        US-only inputs (``census`` / ``lodes_*``) may be omitted for non-US cities.
        """
        ...


class UsCensusLodesProvider:
    """Default provider: Geofabrik OSM + Census boundary/blocks + LODES employment.

    Writes each file into *out_dir* under a content-addressed ``{role}-{hash}{ext}``
    name (via :func:`bikescore_bna.data_pool.store_file`), so a re-acquire of identical
    bytes is idempotent. Regional PBFs are cached and shared in *pbf_cache_dir*.
    """

    def __init__(self, pbf_cache_dir: Path | None = None) -> None:
        self._pbf_cache_dir = pbf_cache_dir

    def _config(self, out_dir: Path, force: bool) -> AcquireConfig:
        cache = self._pbf_cache_dir if self._pbf_cache_dir is not None else _default_pbf_cache_dir()
        return AcquireConfig(
            project_data_dir=out_dir, pbf_cache_dir=cache, force_download=force,
        )

    def acquire(
        self, city: CityIdentity, out_dir: Path, *, force: bool = False,
    ) -> dict[str, Path]:
        import tempfile

        import geopandas as gpd

        from bikescore_bna.stages.parse import pre_clip_pbf

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        config = self._config(out_dir, force)
        result: dict[str, Path] = {}

        with tempfile.TemporaryDirectory() as _tmp:
            tmp_dir = Path(_tmp)

            # ── Boundary ──────────────────────────────────────────────────
            boundary_tmp = _fetch_boundary_tmp(city, tmp_dir)
            boundary_gdf = gpd.read_file(boundary_tmp)
            if boundary_gdf.crs is None or boundary_gdf.crs.to_epsg() != 4326:
                boundary_gdf = boundary_gdf.to_crs(epsg=4326)
            result["boundary"] = self._store(config, "boundary", boundary_tmp, ".geojson")

            # ── Regional PBF → clip to boundary ───────────────────────────
            region_pbf, _ = _download_state_pbf(city, config)
            clipped_tmp = pre_clip_pbf(region_pbf, boundary_gdf, 0.0, tmp_dir)
            result["osm"] = self._store(config, "osm", clipped_tmp, ".pbf")

            # ── Census + LODES (US only) ──────────────────────────────────
            if city.is_us and city.fips_code is not None:
                state_fips = city.fips_code[:2]
                census_tmp = _download_census_blocks_tmp(state_fips, boundary_gdf, tmp_dir)
                if census_tmp is not None:
                    result["census"] = self._store(config, "census", census_tmp, ".parquet")

                state_abbr = _state_abbr_from_fips(state_fips)
                if state_abbr:
                    year = _lodes_latest_year(state_abbr, _LODES_BASE)
                    main_tmp, aux_tmp = _download_lodes_tmp(state_abbr, year, tmp_dir)
                    if main_tmp is not None:
                        result["lodes_main"] = self._store(config, "lodes_main", main_tmp, ".csv")
                    if aux_tmp is not None:
                        result["lodes_aux"] = self._store(config, "lodes_aux", aux_tmp, ".csv")

        return result

    @staticmethod
    def _store(config: AcquireConfig, role: str, tmp_path: Path, ext: str) -> Path:
        name = store_file(config.project_data_dir, role, tmp_path, ext)
        _logger.info("acquire  %s saved: %s", role, name)
        return config.project_data_dir / name


_DEFAULT_PROVIDER = UsCensusLodesProvider()


def acquire_city(
    city: CityIdentity,
    out_dir: Path = Path("./data"),
    *,
    pbf_cache_dir: Path | None = None,
    force: bool = False,
    provider: InputProvider | None = None,
) -> dict[str, Path]:
    """Acquire the raw inputs ``score_city`` needs for *city*, DB-free.

    Args:
        city: The city to acquire data for (drives boundary/PBF/census/LODES sources).
        out_dir: Directory the input files are written into (content-addressed names).
        pbf_cache_dir: Shared regional-PBF cache; defaults to the global settings path.
            Ignored when a custom *provider* is supplied.
        force: Re-download the regional PBF even on a cache hit.
        provider: Override the default US census/LODES :class:`InputProvider`.

    Returns:
        ``{name: path}`` covering ``osm`` / ``boundary`` and (US cities) ``census`` /
        ``lodes_main`` / ``lodes_aux`` — the ``inputs`` dict ``score_city`` expects.
    """
    prov = provider if provider is not None else UsCensusLodesProvider(pbf_cache_dir)
    return prov.acquire(city, Path(out_dir), force=force)


# Role -> glob for the content-addressed files acquire_city writes into a directory.
_INPUT_GLOBS: dict[str, str] = {
    "osm": "osm-*.pbf",
    "boundary": "boundary-*.geojson",
    "census": "census-*.parquet",
    "lodes_main": "lodes_main-*.csv",
    "lodes_aux": "lodes_aux-*.csv",
}


def discover_inputs(datasets_dir: Path | str) -> dict[str, Path]:
    """Map ``acquire_city``'s files in *datasets_dir* to the ``{role: Path}`` score_city wants.

    Reads the role-named outputs (``osm-*.pbf``, ``boundary-*.geojson``, ``census-*.parquet``,
    ``lodes_main-*.csv``, ``lodes_aux-*.csv``). Roles with no matching file are omitted
    (non-US cities have no census/LODES). Stateless directory I/O — no registry, first
    match per role. Loop it over several directories to score several input sets:

        for d in dirs:
            score_city(discover_inputs(d), config)
    """
    d = Path(datasets_dir)
    inputs: dict[str, Path] = {}
    for role, pattern in _INPUT_GLOBS.items():
        hits = sorted(d.glob(pattern))
        if hits:
            inputs[role] = hits[0]
    return inputs
