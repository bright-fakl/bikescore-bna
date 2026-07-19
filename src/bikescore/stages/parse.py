"""Parse stage: read OSM PBF → ways_df, nodes_df, poi_raw.

SQL equivalent: osm2pgsql import + prepare_tables.sql column setup.

A single osmium pass collects:
- Highway ways (highway=* OR bicycle=*) with all tag columns needed by
  attributes and stress stages.
- Intersection nodes — all nodes referenced by highway ways, plus standalone
  nodes that carry signal / stop / RRFB / island attributes.
- Destination POIs — nodes and closed ways matched against the configured
  destination type matchers.

Performance — PBF pre-clipping
-------------------------------
``pre_clip_pbf`` clips the input PBF to the exact city boundary polygon before
the main osmium pass, matching brokenspoke-analyzer's prepare phase
(``osmium extract -p polygon_file``). The scoring core's ``parse`` StageSpec
consumes an already-clipped city PBF directly; ``pre_clip_pbf`` lives here for
``acquire`` (Phase 38g) to call during data acquisition.

  osmium CLI (preferred):
    osmium extract --strategy=complete_ways -p boundary.geojson \\
        input.pbf -o city.pbf --overwrite

  pyosmium BackReferenceWriter fallback (~115 s):
    two-pass: collect in-bbox node IDs, write touching ways.

``complete_ways`` strategy keeps all nodes of any way that has at least one
node inside the polygon — this is important for destination POIs on crossing
ways (e.g. ferry terminals just outside the city on routes into the city).

The clipped PBF is cached in ``config.cache.cache_dir / "pbf"`` and reused
as long as the source PBF is unchanged (mtime comparison).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import osmium
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

_logger = logging.getLogger("bikescore")

STAGE_VERSION: str = "1.0.0"

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore.config import BNAConfig

# ── PBF pre-clip helpers ──────────────────────────────────────────────────────


def _buffered_wgs84(boundary: gpd.GeoDataFrame, buffer_m: float):
    """Return the buffered boundary union as a Shapely geometry in EPSG:4326."""
    import geopandas as gpd
    from shapely.ops import unary_union

    geom = unary_union(boundary.geometry)
    gs = gpd.GeoSeries([geom], crs="EPSG:4326")
    utm = gs.estimate_utm_crs()
    return gs.to_crs(utm).buffer(buffer_m).to_crs("EPSG:4326").iloc[0]


def _clip_with_osmium_cli(
    osmium_bin: str,
    pbf_path: Path,
    boundary_geom: object,
    out_pbf: Path,
) -> None:
    """Clip PBF using the osmium CLI (fast C++ path, ~14 s for a state PBF).

    Uses ``--strategy=complete_ways`` so ways that cross the boundary edge are
    included in full (all their nodes are written), matching the SQL pipeline's
    behaviour.
    """
    from shapely.geometry import mapping

    poly_gj = json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": mapping(boundary_geom), "properties": {}}],
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
        f.write(poly_gj)
        poly_path = f.name

    try:
        result = subprocess.run(
            [
                osmium_bin, "extract",
                "--strategy=complete_ways",
                "-p", poly_path,
                str(pbf_path),
                "-o", str(out_pbf),
                "--overwrite",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        _ = result  # consumed to satisfy linters
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"osmium extract failed:\n{exc.stderr}"
        ) from exc
    finally:
        os.unlink(poly_path)


def _clip_with_pyosmium(
    pbf_path: Path,
    boundary_geom: object,
    out_pbf: Path,
) -> None:
    """Clip PBF using pyosmium BackReferenceWriter (pure Python, ~115 s).

    Two-pass approach:
      Pass 1 — scan nodes, collect IDs whose location falls inside the
               bounding box of the buffered boundary.
      Pass 2 — write ways that reference at least one in-bbox node;
               BackReferenceWriter automatically pulls in all referenced
               node locations from the source file.
    """
    minlon, minlat, maxlon, maxlat = boundary_geom.bounds

    _logger.info("parse  pyosmium pre-clip pass 1/2: collecting bbox node IDs …")
    bbox_node_ids: set[int] = set()
    for n in osmium.FileProcessor(str(pbf_path), osmium.osm.NODE):
        if n.location.valid():
            lon, lat = n.location.lon, n.location.lat
            if minlon <= lon <= maxlon and minlat <= lat <= maxlat:
                bbox_node_ids.add(n.id)
    _logger.info("parse  %d bbox nodes found", len(bbox_node_ids))

    _logger.info("parse  pyosmium pre-clip pass 2/2: writing city PBF …")
    with osmium.BackReferenceWriter(str(out_pbf), ref_src=str(pbf_path), overwrite=True) as writer:
        for w in osmium.FileProcessor(str(pbf_path), osmium.osm.WAY):
            if any(nd.ref in bbox_node_ids for nd in w.nodes):
                writer.add_way(w)


def pre_clip_pbf(
    pbf_path: Path,
    boundary: gpd.GeoDataFrame,
    buffer_m: float,
    cache_dir: Path,
) -> Path:
    """Clip a PBF to the buffered city boundary and cache the result.

    Clips to ``ST_Buffer(boundary, buffer_m)`` using osmium CLI or pyosmium.
    The cached file is reused as long as the source PBF is unchanged (mtime).

    Args:
        pbf_path: PBF to clip.
        boundary: City boundary GeoDataFrame (EPSG:4326).
        buffer_m: Buffer radius in metres (typically config.max_trip_distance).
        cache_dir: Directory for the cached clipped PBF.

    Returns:
        Path to the clipped PBF.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_pbf = cache_dir / f"{pbf_path.stem}.clipped.pbf"

    # Cache hit
    if out_pbf.exists() and out_pbf.stat().st_mtime >= pbf_path.stat().st_mtime:
        size_kb = out_pbf.stat().st_size / 1024
        _logger.info("parse  city PBF cache hit: %s (%.0f KB)", out_pbf.name, size_kb)
        return out_pbf

    buffered = _buffered_wgs84(boundary, buffer_m)
    osmium_bin = shutil.which("osmium")

    if osmium_bin:
        _logger.info("parse  clipping PBF with osmium CLI → %s", out_pbf.name)
        _clip_with_osmium_cli(osmium_bin, pbf_path, buffered, out_pbf)
    else:
        _logger.info("parse  osmium CLI not found — using pyosmium → %s", out_pbf.name)
        _clip_with_pyosmium(pbf_path, buffered, out_pbf)

    size_kb = out_pbf.stat().st_size / 1024
    _logger.info("parse  city PBF written: %s (%.0f KB)", out_pbf.name, size_kb)
    return out_pbf

# ── OSM tag extraction list ───────────────────────────────────────────────────

#: The minimal routing/filtering core Parse needs *intrinsically* — before any
#: attribute runs — to build and prune the road network (way filtering, the
#: ``bicycle=no AND highway=path`` row drop, directed-edge routing). Every other
#: tag is loaded via a primary or derived attribute's ``extra_tags`` (Phase 35g):
#: the functional_class inputs (footway/tracktype/golf/golf_cart) and ``width`` now
#: live as primary attributes in the scenario, so the effective tag set Parse loads
#: is unchanged while the load is explicit and replaceable.
BASE_WAY_TAGS: tuple[str, ...] = (
    # Parse's irreducible core: ``highway``/``bicycle`` drive way filtering
    # (``is_road``, the ``bicycle=no AND highway=path`` row drop) *before* any
    # attribute runs; ``name`` is the reserved output-label column. Everything else
    # a rule/provider consumes — including access/oneway/oneway:bicycle — is loaded
    # via a primary attribute in the scenario (Phase 35g).
    "highway", "bicycle", "name",
)

# ── RawPOI dataclass ──────────────────────────────────────────────────────────

@dataclass
class RawPOI:
    """Unprocessed destination POI from parse stage.

    A single OSM feature (node or closed way) can match multiple destination
    types simultaneously — `matched_types` lists all that matched.
    The destinations stage applies exclude_matchers and clusters per type.
    """

    geometry: Point | Polygon
    matched_types: list[str] = field(default_factory=list)
    osm_id: int = 0
    tags: dict[str, str] = field(default_factory=dict)


# ── Osmium handler ────────────────────────────────────────────────────────────

class _ParseHandler(osmium.SimpleHandler):
    """Single-pass osmium handler.

    Collects highway ways, intersection-relevant nodes, and destination POIs.
    Requires ``apply_file(locations=True)`` so node coordinates are available
    during way processing.
    """

    def __init__(
        self,
        dest_types: list,
        way_tags: tuple[str, ...],
        attr_matchers: list[tuple[str, Any]],
    ) -> None:
        super().__init__()
        self._dest_types = dest_types
        self._way_tags = way_tags
        # Active intersection attributes: list of (name, Matcher)
        self._attr_matchers = attr_matchers
        # Way records — list of dicts, one per valid highway way
        self._ways: list[dict] = []
        # All road nodes: node_id → (lon, lat)
        # Built during way() callback — only nodes referenced by highway ways
        self._road_nodes: dict[int, tuple[float, float]] = {}
        # Special (intersection-attribute) nodes: node_id → attribute dict
        # Built during node() callback
        self._special_nodes: dict[int, dict] = {}
        # Destination POIs
        self._poi: list[RawPOI] = []

    def node(self, n: osmium.osm.Node) -> None:  # type: ignore[name-defined]
        """Collect intersection-attribute nodes and POI point features."""
        if not n.location.valid():
            return
        tags: dict[str, str] = dict(n.tags)

        # Intersection attributes — evaluate each configured matcher against the
        # node's tags (replaces the formerly hard-coded signalized/stop/rrfb/island
        # checks; the four BNA defaults reproduce the same booleans).
        attrs = {name: matcher.matches(tags) for name, matcher in self._attr_matchers}
        if any(attrs.values()):
            self._special_nodes[n.id] = {
                "lon": n.location.lon,
                "lat": n.location.lat,
                **attrs,
            }

        # POI point feature matching
        if self._dest_types:
            matched: list[str] = []
            for dt in self._dest_types:
                if dt.node_match.matches(tags):
                    matched.append(dt.name)
            if matched:
                self._poi.append(RawPOI(
                    geometry=Point(n.location.lon, n.location.lat),
                    matched_types=matched,
                    osm_id=n.id,
                    tags=tags,
                ))

    def way(self, w: osmium.osm.Way) -> None:  # type: ignore[name-defined]
        """Collect highway way geometry, tags, and POI polygon features."""
        tags: dict[str, str] = dict(w.tags)
        hw = tags.get("highway")
        bicycle = tags.get("bicycle")
        is_road = hw or bicycle

        # POI closed-way matching: scan ALL closed ways (not just highway ways).
        # Destination polygons (parks, schools, universities, etc.) are not highway
        # tagged — they would be missed if we only scan road ways.
        is_closed = len(w.nodes) >= 4 and w.nodes[0].ref == w.nodes[-1].ref
        if self._dest_types and is_closed:
            matched: list[str] = []
            for dt in self._dest_types:
                if dt.area_match.matches(tags):
                    matched.append(dt.name)
            if matched:
                try:
                    coords_poi = []
                    for nd in w.nodes:
                        try:
                            coords_poi.append((nd.lon, nd.lat))
                        except osmium.InvalidLocationError:
                            pass
                    if len(coords_poi) >= 4:
                        poly = Polygon(coords_poi)
                        if not poly.is_valid:
                            poly = poly.buffer(0)
                        if poly.is_valid and not poly.is_empty:
                            self._poi.append(RawPOI(
                                geometry=poly,
                                matched_types=matched,
                                osm_id=w.id,
                                tags=tags,
                            ))
                except Exception:
                    pass

        # Skip non-road ways for road network processing
        if not is_road:
            return

        # Collect valid nodes (skip nodes with missing location data)
        valid: list[tuple[int, float, float]] = []
        try:
            for nd in w.nodes:
                try:
                    lon, lat = nd.lon, nd.lat
                    valid.append((nd.ref, lon, lat))
                except osmium.InvalidLocationError:
                    pass
        except Exception:
            return

        if len(valid) < 2:
            return

        coords = [(lon, lat) for _, lon, lat in valid]
        node_ids = [ref for ref, _, _ in valid]

        # Store road node locations for nodes_df construction
        for ref, lon, lat in valid:
            self._road_nodes[ref] = (lon, lat)

        # Build way record
        self._ways.append({
            "osm_id": w.id,
            "node_ids": node_ids,
            "geometry": LineString(coords),
            **{t: tags.get(t) for t in self._way_tags},
        })


    def area(self, a: osmium.osm.Area) -> None:  # type: ignore[name-defined]
        """Handle assembled multipolygon relation areas (parks, schools, campuses, etc.)."""
        if a.from_way():
            return  # Already captured as closed way in way(); skip to avoid double-count

        if not self._dest_types:
            return

        tags: dict[str, str] = dict(a.tags)
        matched: list[str] = []
        for dt in self._dest_types:
            if dt.area_match.matches(tags):
                matched.append(dt.name)
        if not matched:
            return

        try:
            import osmium.geom
            from shapely.wkb import loads as wkb_loads
            fab = osmium.geom.WKBFactory()
            wkb = fab.create_multipolygon(a)
            geom = wkb_loads(bytes.fromhex(wkb))
            if geom.geom_type == "MultiPolygon":
                # Store each outer ring as a separate Polygon so boundary clip
                # (centroid-contains) applies per component — matching how
                # osm2pgsql stores each outer ring as an individual polygon record.
                components = list(geom.geoms)
            elif geom.geom_type == "Polygon":
                components = [geom]
            else:
                return
            for comp in components:
                if not comp.is_valid:
                    comp = comp.buffer(0)
                if comp.is_valid and not comp.is_empty:
                    self._poi.append(RawPOI(
                        geometry=comp,
                        matched_types=matched,
                        osm_id=a.orig_id(),
                        tags=tags,
                    ))
        except Exception:
            pass


# ── Length computation helper ─────────────────────────────────────────────────

def _compute_lengths(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Compute geodetic length in metres for each way geometry.

    Projects to the local UTM zone for accurate metric distances.
    Returns a Series of floats indexed like gdf.
    """
    if len(gdf) == 0:
        return pd.Series(dtype=float)
    utm_crs = gdf.geometry.dropna().iloc[:1].estimate_utm_crs() if not gdf.geometry.dropna().empty else None
    if utm_crs is None:
        return pd.Series([float("nan")] * len(gdf), index=gdf.index)
    gdf_utm = gdf.to_crs(utm_crs)
    return gdf_utm.geometry.length


# ── Public API ────────────────────────────────────────────────────────────────

def parse(
    osm_pbf: Path,
    boundary: gpd.GeoDataFrame,
    config: BNAConfig,
    way_tags: tuple[str, ...] = BASE_WAY_TAGS,
    intersection_attributes: list | None = None,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, list[RawPOI]]:
    """Parse an OSM PBF file into ways, nodes, and raw POIs.

    Args:
        osm_pbf: Path to the OSM PBF file (state or city extract).
        boundary: City boundary GeoDataFrame (any geometry type; CRS = EPSG:4326).
        config: Pipeline configuration; config.destinations used for POI matching.
        way_tags: Tags to extract as columns from highway ways. Defaults to
            BASE_WAY_TAGS; callers should pass the effective set (BASE_WAY_TAGS +
            attribute extra_tags) so all needed tag columns are present.
        intersection_attributes: Active list[IntersectionAttribute] driving the
            boolean node-attribute columns. Defaults to the four BNA attributes
            (signalized, stop, rrfb, island) when None.

    Returns:
        ways_df: GeoDataFrame — one row per highway way. CRS = EPSG:4326.
            Columns: osm_id, geometry, node_ids, name, highway, length_m,
            plus all tag columns in way_tags.
        nodes_df: DataFrame — one row per road node (referenced by ways) plus
            standalone intersection-attribute nodes.
            Columns: node_id, lon, lat, plus one boolean column per active
            intersection attribute (signalized, stop, rrfb, island by default).
        poi_raw: list[RawPOI] — unfiltered destination POIs.
    """
    import geopandas as gpd

    if intersection_attributes is None:
        from bikescore.intersection_attributes import default_intersection_attributes
        intersection_attributes = default_intersection_attributes()
    active_attrs = [a for a in intersection_attributes if a.enabled]
    attr_names = [a.name for a in active_attrs]
    attr_matchers = [(a.name, a.match) for a in active_attrs]

    dest_registry = config.destinations
    if dest_registry is None:
        from bikescore.destinations import default_destination_registry
        dest_registry = default_destination_registry()
    # network_path entries (trails) carry no node/area matchers — exclude them from
    # POI extraction; they are detected by the segment stage from way_match.
    dest_types = [dt for dt in dest_registry.active() if dt.type != "network_path"]

    # Pool files (from bna acquire) are already pre-clipped city PBFs; parse
    # consumes them directly. pre_clip_pbf is still available in this module
    # and is called by acquire.py during data acquisition.
    pbf_to_parse = osm_pbf

    _logger.info(
        "parse  file=%s  dest_types=%d  attrs=%s",
        osm_pbf.name, len(dest_types), ",".join(attr_names),
    )
    handler = _ParseHandler(dest_types, way_tags, attr_matchers)
    handler.apply_file(str(pbf_to_parse), locations=True)
    _logger.info(
        "parse  raw — ways=%d  road_nodes=%d  special_nodes=%d  poi=%d",
        len(handler._ways), len(handler._road_nodes),
        len(handler._special_nodes), len(handler._poi),
    )
    if handler._special_nodes:
        _counts = {
            name: sum(1 for v in handler._special_nodes.values() if v.get(name))
            for name in attr_names
        }
        _logger.info(
            "parse  special_nodes — %s",
            "  ".join(f"{k}={v}" for k, v in _counts.items()),
        )

    # ── Build ways_df ─────────────────────────────────────────────────────────
    if handler._ways:
        ways_df = gpd.GeoDataFrame(handler._ways, crs="EPSG:4326")
        # Rename way_id → osm_id if needed (bna-python compat)
        if "way_id" in ways_df.columns and "osm_id" not in ways_df.columns:
            ways_df = ways_df.rename(columns={"way_id": "osm_id"})
        ways_df["length_m"] = _compute_lengths(ways_df)
    else:
        cols = ["osm_id", "geometry", "node_ids", "length_m", *way_tags]
        ways_df = gpd.GeoDataFrame(columns=cols, crs="EPSG:4326")

    # ── Build nodes_df ────────────────────────────────────────────────────────
    node_records: list[dict] = []
    for nid, (lon, lat) in handler._road_nodes.items():
        special = handler._special_nodes.get(nid, {})
        rec = {"node_id": nid, "lon": lon, "lat": lat}
        for name in attr_names:
            rec[name] = special.get(name, False)
        node_records.append(rec)
    # Add special nodes not referenced by any road way
    for nid, attrs in handler._special_nodes.items():
        if nid not in handler._road_nodes:
            rec = {"node_id": nid, "lon": attrs["lon"], "lat": attrs["lat"]}
            for name in attr_names:
                rec[name] = attrs.get(name, False)
            node_records.append(rec)

    if node_records:
        nodes_df = pd.DataFrame(node_records)
        nodes_df = nodes_df.drop_duplicates(subset="node_id").reset_index(drop=True)
    else:
        nodes_df = pd.DataFrame(columns=["node_id", "lon", "lat", *attr_names])

    if handler._poi:
        from collections import Counter
        _poi_counts = Counter(t for p in handler._poi for t in p.matched_types)
        _logger.info(
            "parse  poi_by_type — %s",
            "  ".join(f"{k}={v}" for k, v in sorted(_poi_counts.items())),
        )
    _n_with_attrs = sum(
        1 for r in node_records
        if any(r.get(name) for name in attr_names)
    )
    # Remove footpaths that explicitly prohibit cycling — they are not part of
    # the routing network. (Was previously done in the clip stage.)
    if len(ways_df) > 0 and "bicycle" in ways_df.columns and "highway" in ways_df.columns:
        remove_mask = (ways_df["bicycle"] == "no") & (ways_df["highway"] == "path")
        n_removed = int(remove_mask.sum())
        if n_removed:
            ways_df = ways_df[~remove_mask].reset_index(drop=True)
            _logger.info("parse  removed bicycle=no/highway=path: %d", n_removed)

    _logger.info(
        "parse  output — ways=%d  road_nodes=%d (all way vertices, %d with intersection attrs)",
        len(ways_df), len(nodes_df), _n_with_attrs,
    )

    return ways_df, nodes_df, handler._poi


# ── StageSpec wrapper ─────────────────────────────────────────────────────────

from bikescore.stage import StageSpec  # noqa: E402


def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
    import pickle

    import geopandas as gpd

    osm_path = Path(input_paths["dataset:osm"])
    boundary = gpd.read_file(input_paths["dataset:boundary"]).to_crs(epsg=4326)

    attribute_tags = (
        sorted(config.attributes.extra_osm_tags() - set(BASE_WAY_TAGS))
        if config.attributes is not None else []
    )
    effective_tags = BASE_WAY_TAGS + tuple(attribute_tags)
    ways_df, nodes_df, poi_raw = parse(
        osm_path, boundary, config,
        way_tags=effective_tags,
        intersection_attributes=config.intersection_attributes,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ways_df.to_parquet(out / "ways_raw.parquet")
    nodes_df.to_parquet(out / "nodes.parquet")
    with open(out / "poi_raw.pkl", "wb") as fh:
        pickle.dump(poi_raw, fh)


PARSE = StageSpec(
    name="parse",
    depends_on=(),
    dataset_inputs=("osm", "boundary"),
    version=STAGE_VERSION,
    run=_run,
)
