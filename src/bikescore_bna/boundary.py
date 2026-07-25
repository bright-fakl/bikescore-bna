"""Analysis-boundary transforms applied once, at acquire time.

``prepare_boundary`` turns the *fetched* city boundary (the source / provenance
polygon) into the **analysis** boundary that ``parse`` / ``census`` / ``segment``
consume. Transforms run in a fixed order:

    make_valid → keep_largest_part → fill_holes → convex_hull → clip

``make_valid`` is unconditional hygiene, but is a no-op on already-valid input. When
no transform is configured *and* the source is valid, :func:`prepare_boundary` returns
the **same object** unchanged — the caller can then reuse the source file byte-for-byte
(oracle parity: the analysis boundary is identical to the fetched one).

The subsetting / hole-filling transforms (``keep_largest_part``, ``fill_holes``, the
box/circle clip) each shrink the extent or fill interior holes, so the result stays
within the already-downloaded regional data. ``convex_hull`` can bulge the extent
*outward*; when it (or a non-zero network buffer, or an ``override_geometry`` source)
pushes past the fetched region, acquire's coverage detector requires the missing
regions in ``extra_regions``. ``override_geometry`` is resolved by acquire as a source
replacement *before* this function runs — it is not a transform here.

All metric operations (clip sizing) run in a projected CRS — ``config.output_srid`` when
set, else a UTM estimate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import geopandas as gpd
    from pyproj import CRS
    from shapely.geometry.base import BaseGeometry

    from bikescore_bna.config import BNAConfig, BoundaryConfig

_WGS84 = 4326


def has_boundary_transforms(bc: BoundaryConfig) -> bool:
    """True when any explicit boundary transform is configured.

    ``make_valid`` is not counted — it is unconditional hygiene, a no-op on valid
    input. Used to decide whether the analysis boundary can share the source file.

    ``override_geometry`` is not counted here either: it is a *source* replacement
    resolved by acquire before ``prepare_boundary`` runs, not a transform of the source.
    """
    return bool(
        bc.fill_holes
        or bc.keep_largest_part
        or bc.convex_hull
        or bc.clip_shape is not None
    )


def prepare_boundary(source_gdf: gpd.GeoDataFrame, config: BNAConfig) -> gpd.GeoDataFrame:
    """Derive the analysis boundary from the fetched *source* boundary.

    Args:
        source_gdf: The fetched city boundary (any CRS; treated as WGS84 lon/lat when
            it has no CRS — matching how ``acquire`` supplies it in EPSG:4326).
        config: The effective :class:`BNAConfig`; reads ``config.boundary`` and
            ``config.output_srid``.

    Returns:
        The analysis boundary as a single-row GeoDataFrame in EPSG:4326. When no
        transform is configured and the source is already valid, the **same object**
        (``source_gdf``) is returned unchanged, so callers can detect identity via
        ``result is source_gdf`` and preserve byte-for-byte parity.
    """
    import geopandas as gpd

    bc = config.boundary
    src = source_gdf if source_gdf.crs is not None else source_gdf.set_crs(epsg=_WGS84)

    all_valid = bool(src.geometry.is_valid.all())
    if not has_boundary_transforms(bc) and all_valid:
        # Identity fast-path: no transform + valid source ⇒ analysis == source.
        return source_gdf

    src = src.to_crs(epsg=_WGS84)

    # make_valid (always) → dissolve to one working geometry.
    geom = _make_valid_union(src)

    if bc.keep_largest_part:
        geom = _keep_largest_part(geom)
    if bc.fill_holes:
        geom = _fill_holes(geom)
    if bc.convex_hull:
        geom = geom.convex_hull
    if bc.clip_shape is not None:
        metric_crs = _metric_crs(src, config)
        geom = _clip(geom, bc.clip_shape, float(bc.clip_size_m), src.crs, metric_crs)

    return gpd.GeoDataFrame(geometry=[geom], crs=src.crs)


def load_override_geometry(override: str | object) -> gpd.GeoDataFrame:
    """Load a ``boundary.override_geometry`` source into a single-row WGS84 GeoDataFrame.

    ``override`` is either a filesystem path to a GeoJSON/vector file (dissolved to one
    geometry) or an inline WGS84 bounding box ``[minx, miny, maxx, maxy]``. This is a
    *source* replacement for the fetched city boundary — the result becomes both the
    provenance ``boundary`` and the input to :func:`prepare_boundary`.
    """
    import geopandas as gpd
    from shapely.geometry import box
    from shapely.ops import unary_union

    if isinstance(override, (list, tuple)):
        minx, miny, maxx, maxy = (float(v) for v in override)
        return gpd.GeoDataFrame(geometry=[box(minx, miny, maxx, maxy)], crs=_WGS84)

    gdf = gpd.read_file(override)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=_WGS84)
    elif gdf.crs.to_epsg() != _WGS84:
        gdf = gdf.to_crs(epsg=_WGS84)
    geom = unary_union(gdf.geometry)
    return gpd.GeoDataFrame(geometry=[geom], crs=_WGS84)


# ── Transform helpers ─────────────────────────────────────────────────────────


def _make_valid_union(gdf: gpd.GeoDataFrame):
    """Repair every geometry (``make_valid``) and dissolve into one (multi)polygon."""
    from shapely import make_valid
    from shapely.ops import unary_union

    repaired = [make_valid(g) for g in gdf.geometry if g is not None and not g.is_empty]
    return unary_union(repaired)


def _polygons(geom: BaseGeometry) -> list:
    """Flatten a geometry to its constituent Polygon parts."""
    from shapely.geometry import MultiPolygon, Polygon

    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    # GeometryCollection or other: keep only polygonal parts.
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _keep_largest_part(geom: BaseGeometry) -> BaseGeometry:
    """Drop detached parts, keeping the single largest polygon by area."""
    parts = _polygons(geom)
    if len(parts) <= 1:
        return geom
    return max(parts, key=lambda p: p.area)


def _fill_holes(geom: BaseGeometry) -> BaseGeometry:
    """Rebuild each polygon from its exterior ring, removing all interior holes."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union

    filled = [Polygon(p.exterior) for p in _polygons(geom)]
    if not filled:
        return geom
    if len(filled) == 1:
        return filled[0]
    return unary_union(MultiPolygon(filled))


def _metric_crs(gdf: gpd.GeoDataFrame, config: BNAConfig):
    """Projected CRS for metric ops: ``output_srid`` if set, else a UTM estimate."""
    if config.output_srid is not None:
        return config.output_srid
    return gdf.estimate_utm_crs()


def _clip(
    geom: BaseGeometry, shape: str, size_m: float, src_crs: CRS, metric_crs: CRS | int
) -> BaseGeometry:
    """Clip *geom* to a box/circle of ``size_m`` centered on its centroid, ∩ boundary.

    The clip shape is built and intersected in the projected ``metric_crs`` (so the
    size is a true metric length), then the result is projected back to ``src_crs``.
    """
    import geopandas as gpd
    from shapely.geometry import box

    projected = gpd.GeoSeries([geom], crs=src_crs).to_crs(metric_crs).iloc[0]
    cx, cy = projected.centroid.x, projected.centroid.y
    if shape == "box":
        half = size_m / 2.0
        clip_geom = box(cx - half, cy - half, cx + half, cy + half)
    elif shape == "circle":
        clip_geom = projected.centroid.buffer(size_m / 2.0)
    else:  # pragma: no cover - guarded by BoundaryConfig.validate / Literal type
        raise ValueError(f"unknown clip_shape {shape!r}")

    clipped = projected.intersection(clip_geom)
    return gpd.GeoSeries([clipped], crs=metric_crs).to_crs(src_crs).iloc[0]
