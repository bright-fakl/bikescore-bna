"""Export a ``score_city`` result to consumer formats (GeoJSON / Shapefile / CSV).

This is the database-free counterpart of the app's Phase-26 export registry. The app
resolves a target's stage outputs from its run store (``city_dir`` / ``run_id`` /
``StageService``); the core has no run store, so an :class:`ExportContext` is built
directly from a :class:`bikescore_bna.pipeline.ScoreResult` — the ``{stage_name: output_dir}``
map ``score_city`` already returns — plus the ``inputs`` dict (for dataset-backed targets
like ``boundary``), the :class:`~bikescore_bna.city.CityIdentity`, and the effective
:class:`~bikescore_bna.config.BNAConfig`.

Everything lives in this one module (targets, bundles, writers) rather than registering
via stage import-side-effects: the core deliberately has no import-side-effect registry
(see :mod:`bikescore_bna.stage`), and centralising keeps the whole export surface auditable in
one place. A stage's output filenames are the contract these targets read; keep them in
sync with ``bikescore_bna.stages.*``.

Public surface:

    list_export_targets() -> list[str]
    list_export_bundles() -> list[str]
    build_export_context(result, city, config, *, inputs=None) -> ExportContext
    export_target(result, city, config, name, dest_dir, *, file_format, inputs=None) -> [Path]
    export_bundle(result, city, config, dest_dir, *, bundle="bna", inputs=None, …) -> [Path]
"""

from __future__ import annotations

import logging
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd

if TYPE_CHECKING:
    import geopandas as gpd

    from bikescore_bna.city import CityIdentity
    from bikescore_bna.config import BNAConfig
    from bikescore_bna.pipeline import ScoreResult

_logger = logging.getLogger("bikescore-bna")


# ── Errors ────────────────────────────────────────────────────────────────────


class StageOutputNotFoundError(FileNotFoundError):
    """A target's required stage output (or dataset input) is missing for this result."""


# ── ExportContext ─────────────────────────────────────────────────────────────


@dataclass
class ExportContext:
    """Everything a target's ``build`` callable needs, over a ``score_city`` result.

    Attributes:
        stage_dirs: ``{stage_name: output_dir}`` from :class:`ScoreResult`.
        dataset_paths: ``{role: file_path}`` — the ``inputs`` passed to ``score_city``
            (e.g. ``"osm"``, ``"boundary"``). May be empty; dataset-backed targets then
            fail with :class:`StageOutputNotFoundError`.
        city: City identity metadata (name / region / FIPS …).
        config: The effective config the run used (for config-derived targets).
    """

    stage_dirs: dict[str, Path]
    dataset_paths: dict[str, Path]
    city: CityIdentity
    config: BNAConfig

    def stage_dir(self, stage: str) -> Path:
        """Resolve ``stage``'s output directory. Raises if the stage did not run."""
        out = self.stage_dirs.get(stage)
        if out is None:
            raise StageOutputNotFoundError(
                f"stage {stage!r} was not part of this run "
                f"(ran: {sorted(self.stage_dirs)})"
            )
        return out

    def stage_df(self, stage: str, filename: str, geo: bool = False) -> pd.DataFrame | gpd.GeoDataFrame:
        """Read ``filename`` from ``stage``'s output directory.

        Raises :class:`StageOutputNotFoundError` if the stage did not run, or if the
        directory exists but does not contain ``filename``.
        """
        path = self.stage_dir(stage) / filename
        if not path.exists():
            raise StageOutputNotFoundError(
                f"stage {stage!r} output {filename!r} not found in {path.parent}"
            )
        if geo:
            import geopandas as gpd

            return gpd.read_parquet(path)
        return pd.read_parquet(path)

    def dataset_path(self, name: str) -> Path | None:
        """Resolve a dataset input (e.g. ``"boundary"``); ``None`` if not supplied."""
        return self.dataset_paths.get(name)


def build_export_context(
    result: ScoreResult,
    city: CityIdentity,
    config: BNAConfig,
    *,
    inputs: dict[str, Path] | None = None,
) -> ExportContext:
    """Build an :class:`ExportContext` from a ``score_city`` result + its inputs."""
    return ExportContext(
        stage_dirs=dict(result.stage_dirs),
        dataset_paths=dict(inputs or {}),
        city=city,
        config=config,
    )


# ── Core types ────────────────────────────────────────────────────────────────


DEFAULT_FORMATS: dict[str, list[str]] = {
    "geo": ["geojson", "shapefile", "csv"],
    "plain": ["csv"],
}

BuildFn = Callable[
    [ExportContext],
    "pd.DataFrame | gpd.GeoDataFrame | dict[str, pd.DataFrame | gpd.GeoDataFrame]",
]


@dataclass
class ExportTarget:
    name: str
    owner_stage: str | None
    requires: list[str]
    kind: Literal["geo", "plain"]
    build: BuildFn
    fan_out: bool = False
    formats: list[str] | None = None


@dataclass
class ExportBundle:
    name: str
    targets: dict[str, str]
    formats: dict[str, list[str]] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    write_readme: bool = True


_EXPORT_TARGETS: dict[str, ExportTarget] = {}
_EXPORT_BUNDLES: dict[str, ExportBundle] = {}


# ── Registration ──────────────────────────────────────────────────────────────


def _target(
    name: str,
    owner_stage: str | None,
    requires: list[str],
    kind: Literal["geo", "plain"],
    fan_out: bool = False,
    formats: list[str] | None = None,
):
    """Decorator registering ``fn`` as an :class:`ExportTarget`'s build callable."""

    def decorator(fn: BuildFn) -> BuildFn:
        if name in _EXPORT_TARGETS:
            raise ValueError(f"export target {name!r} is already registered")
        _EXPORT_TARGETS[name] = ExportTarget(
            name=name,
            owner_stage=owner_stage,
            requires=requires,
            kind=kind,
            fan_out=fan_out,
            build=fn,
            formats=formats,
        )
        return fn

    return decorator


def _simple_target(name: str, owner_stage: str, filename: str, kind: Literal["geo", "plain"]) -> None:
    """Register a non-fan-out target that just reads ``filename`` from ``owner_stage``."""
    if name in _EXPORT_TARGETS:
        raise ValueError(f"export target {name!r} is already registered")
    geo = kind == "geo"
    _EXPORT_TARGETS[name] = ExportTarget(
        name=name,
        owner_stage=owner_stage,
        requires=[owner_stage],
        kind=kind,
        fan_out=False,
        build=lambda ctx, _s=owner_stage, _f=filename, _g=geo: ctx.stage_df(_s, _f, geo=_g),
    )


def validate_target_formats(target: ExportTarget, formats: list[str]) -> None:
    """Raise ``ValueError`` if any of ``formats`` is not supported by ``target``."""
    allowed = target.formats or DEFAULT_FORMATS[target.kind]
    for fmt in formats:
        if fmt not in allowed:
            raise ValueError(
                f"target {target.name!r} does not support format {fmt!r}; "
                f"supported: {', '.join(allowed)}"
            )


def _register_bundle(bundle: ExportBundle) -> None:
    """Statically validate ``bundle`` against ``_EXPORT_TARGETS`` and register it."""
    if bundle.name in _EXPORT_BUNDLES:
        raise ValueError(f"export bundle {bundle.name!r} is already registered")

    unknown = set(bundle.targets) - _EXPORT_TARGETS.keys()
    if unknown:
        raise ValueError(f"bundle {bundle.name!r} references unknown target(s): {sorted(unknown)}")
    if not bundle.required <= set(bundle.targets):
        raise ValueError(
            f"bundle {bundle.name!r} 'required' is not a subset of 'targets': "
            f"{sorted(bundle.required - set(bundle.targets))}"
        )

    fixed: dict[str, str] = {}
    for target_name, template in bundle.targets.items():
        target = _EXPORT_TARGETS[target_name]
        if target.fan_out:
            if "{key}" not in template:
                raise ValueError(
                    f"bundle {bundle.name!r}: fan-out target {target_name!r} template "
                    f"{template!r} has no '{{key}}' placeholder"
                )
        else:
            if "{key}" in template:
                raise ValueError(
                    f"bundle {bundle.name!r}: non-fan-out target {target_name!r} template "
                    f"{template!r} contains '{{key}}'"
                )
            if template in fixed:
                raise ValueError(
                    f"bundle {bundle.name!r}: targets {fixed[template]!r} and "
                    f"{target_name!r} both produce filename {template!r}"
                )
            fixed[template] = target_name

    for target_name, fmts in bundle.formats.items():
        validate_target_formats(_EXPORT_TARGETS[target_name], fmts)

    _EXPORT_BUNDLES[bundle.name] = bundle


def list_export_targets() -> list[str]:
    """Sorted names of every registered export target."""
    return sorted(_EXPORT_TARGETS)


def list_export_bundles() -> list[str]:
    """Sorted names of every registered export bundle."""
    return sorted(_EXPORT_BUNDLES)


def target_bundles(name: str) -> list[str]:
    """Names of registered bundles whose ``targets`` include ``name``."""
    return [b.name for b in _EXPORT_BUNDLES.values() if name in b.targets]


# ── Writers ───────────────────────────────────────────────────────────────────


def _to_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject a GeoDataFrame to EPSG:4326 if not already there."""
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        return gdf.to_crs(epsg=4326)
    return gdf


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    _to_wgs84(gdf).to_file(str(path), driver="GeoJSON")


def _write_shapefile(gdf: gpd.GeoDataFrame, path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # geopandas truncates >10-char column names
        _to_wgs84(gdf).to_file(str(path), driver="ESRI Shapefile")


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(str(path), index=False, na_rep="")


def _write(df: pd.DataFrame | gpd.GeoDataFrame, path_stem: Path, fmt: str, geo: bool) -> Path:
    """Dispatch to the format-specific writer, validating ``fmt`` against ``geo``."""
    if fmt == "geojson":
        if not geo:
            raise ValueError(f"format 'geojson' requires a geo target at {path_stem}")
        out = path_stem.with_suffix(".geojson")
        _write_geojson(df, out)
        return out
    if fmt in ("shapefile", "shp"):
        if not geo:
            raise ValueError(f"format 'shapefile' requires a geo target at {path_stem}")
        out = path_stem.with_suffix(".shp")
        _write_shapefile(df, out)
        return out
    if fmt == "csv":
        out = path_stem.with_suffix(".csv")
        _write_csv(df.drop(columns=["geometry"]) if geo else df, out)
        return out
    raise ValueError(f"Unsupported export format: {fmt!r}")


def _write_target(
    target: ExportTarget,
    ctx: ExportContext,
    dest_dir: Path,
    formats: list[str],
    filename_stem: str | None = None,
    reserved_stems: set[str] | None = None,
) -> list[Path]:
    """Build ``target`` and write every ``formats`` entry into ``dest_dir``.

    ``formats`` must already have passed :func:`validate_target_formats`. For fan-out
    targets ``filename_stem`` is a ``{key}`` template; any expanded stem already in
    ``reserved_stems`` is skipped (with a warning), and stems written are added back in.
    """
    result = target.build(ctx)
    if target.fan_out:
        assert isinstance(result, dict), (
            f"target {target.name!r} is fan-out but build() returned {type(result).__name__}"
        )
        template = filename_stem or f"{target.name}_{{key}}"
        if "{key}" not in template:
            raise ValueError(
                f"target {target.name!r} is fan-out but template {template!r} has no '{{key}}'"
            )
        items = {template.format(key=key): df for key, df in result.items()}
    else:
        assert not isinstance(result, dict), (
            f"target {target.name!r} is not fan-out but build() returned a dict"
        )
        items = {(filename_stem or target.name): result}

    geo = target.kind == "geo"
    written: list[Path] = []
    for stem, df in items.items():
        if target.fan_out and reserved_stems is not None and stem in reserved_stems:
            _logger.warning(
                "skipping %r output %r: filename collides with another bundle target",
                target.name, stem,
            )
            continue
        if reserved_stems is not None:
            reserved_stems.add(stem)
        if geo:
            df = _to_wgs84(df)
        for fmt in formats:
            written.append(_write(df, dest_dir / stem, fmt, geo=geo))
    return written


def _resolve_requires(ctx: ExportContext, target: ExportTarget) -> None:
    """Raise :class:`StageOutputNotFoundError` if any of ``target.requires`` is missing."""
    for entry in target.requires:
        if entry.startswith("dataset:"):
            name = entry[len("dataset:"):]
            if ctx.dataset_path(name) is None:
                raise StageOutputNotFoundError(f"dataset {name!r} not supplied for this run")
        else:
            ctx.stage_dir(entry)  # raises if the stage did not run


# ── Public export functions ───────────────────────────────────────────────────


def export_target(
    result: ScoreResult,
    city: CityIdentity,
    config: BNAConfig,
    name: str,
    dest_dir: Path,
    *,
    file_format: str,
    inputs: dict[str, Path] | None = None,
) -> list[Path]:
    """Export a single target from a ``score_city`` result, flat into ``dest_dir``.

    Args:
        result: The :class:`ScoreResult` from ``score_city``.
        city, config: The identity and effective config the run used.
        name: A target name (see :func:`list_export_targets`).
        dest_dir: Directory to write into (created if absent).
        file_format: ``geojson`` / ``shapefile`` / ``csv`` (must suit the target).
        inputs: The ``inputs`` dict passed to ``score_city`` (needed by dataset-backed
            targets such as ``boundary``).

    Raises:
        ValueError: Unknown target or unsupported format.
        StageOutputNotFoundError: A required stage/dataset input is missing.
    """
    if name not in _EXPORT_TARGETS:
        raise ValueError(f"unknown export target {name!r}; known: {', '.join(list_export_targets())}")
    target = _EXPORT_TARGETS[name]
    validate_target_formats(target, [file_format])

    ctx = build_export_context(result, city, config, inputs=inputs)
    _resolve_requires(ctx, target)

    dest_dir.mkdir(parents=True, exist_ok=True)
    return _write_target(target, ctx, dest_dir, [file_format])


def export_bundle(
    result: ScoreResult,
    city: CityIdentity,
    config: BNAConfig,
    dest_dir: Path,
    *,
    bundle: str = "bna",
    inputs: dict[str, Path] | None = None,
    write_readme: bool = True,
) -> list[Path]:
    """Export a named bundle of targets into ``dest_dir``.

    Optional (non-``required``) targets whose inputs are missing are skipped with a
    warning; a missing ``required`` target raises. When ``write_readme`` is set, a
    self-describing ``README.md`` is written alongside the outputs.

    Raises:
        ValueError: Unknown bundle.
        StageOutputNotFoundError: A ``required`` target's stage/dataset input is missing.
    """
    if bundle not in _EXPORT_BUNDLES:
        raise ValueError(f"unknown export bundle {bundle!r}; known: {', '.join(list_export_bundles())}")
    spec = _EXPORT_BUNDLES[bundle]

    ctx = build_export_context(result, city, config, inputs=inputs)
    dest_dir.mkdir(parents=True, exist_ok=True)

    reserved = {tpl for name, tpl in spec.targets.items() if not _EXPORT_TARGETS[name].fan_out}

    written: list[Path] = []
    skipped: list[str] = []
    for target_name, stem in spec.targets.items():
        target = _EXPORT_TARGETS[target_name]
        formats = spec.formats.get(target_name) or target.formats or DEFAULT_FORMATS[target.kind]
        try:
            _resolve_requires(ctx, target)
            written += _write_target(target, ctx, dest_dir, formats, stem, reserved)
        except StageOutputNotFoundError as exc:
            if target_name in spec.required:
                raise
            message = f"skipping {target_name}: {exc}"
            _logger.warning(message)
            skipped.append(message)

    if spec.write_readme:
        _write_readme(dest_dir, city, skipped=skipped or None)

    return written


# ── README ────────────────────────────────────────────────────────────────────


def _write_readme(out_dir: Path, city: CityIdentity, *, skipped: list[str] | None = None) -> None:
    """Write a self-describing ``README.md`` naming the city, version, and files."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        ver = pkg_version("bikescore-bna")
    except PackageNotFoundError:
        ver = "dev"

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# BNA Analysis: {city.name}",
        f"Generated by bikescore-bna {ver} — {now}",
        "",
        "## City",
        "| Field | Value |",
        "|---|---|",
        f"| Name | {city.name} |",
        f"| Country | {city.country} |",
    ]
    if city.region:
        lines.append(f"| Region | {city.region} |")
    if city.fips_code:
        lines.append(f"| FIPS | {city.fips_code} |")

    if skipped:
        lines += ["", "## Skipped outputs", "| Reason |", "|---|"]
        lines += [f"| {reason} |" for reason in skipped]

    files = sorted(f.name for f in out_dir.iterdir() if f.is_file() and f.name != "README.md")
    if files:
        lines += ["", "## Export Files", "| File |", "|---|"]
        lines += [f"| `{f}` |" for f in files]

    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Geometry / formatting build helpers ──────────────────────────────────────


def _build_census_blocks_gdf(block_scores_df: pd.DataFrame, blocks_df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Merge block scores onto block geometries for export."""
    import geopandas as gpd

    merged = blocks_df.merge(block_scores_df, on="geoid20", how="left")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=blocks_df.crs)


def _build_intersections_gdf(
    nodes_df: pd.DataFrame,
    segments_df: gpd.GeoDataFrame | pd.DataFrame,
) -> gpd.GeoDataFrame:
    """Build the ``neighborhood_ways_intersections`` point GeoDataFrame.

    ``legs`` is the count of segment endpoints (``start_node_id`` / ``end_node_id``) at
    each node; only segment-endpoint nodes are kept, and ``stop`` is renamed ``stops``.
    Trails are excluded (they are not part of the routable ``ways`` topology). An empty
    ``segments_df`` yields an empty GeoDataFrame with the full output schema.
    """
    import geopandas as gpd

    if "lon" in nodes_df.columns and "lat" in nodes_df.columns:
        lon_col, lat_col = "lon", "lat"
    elif "x" in nodes_df.columns and "y" in nodes_df.columns:
        lon_col, lat_col = "x", "y"
    else:
        raise ValueError(f"nodes_df missing lon/lat columns; got {list(nodes_df.columns)}")

    if segments_df.empty:
        columns = ["node_id", "lon", "lat", "legs", "stops", "signalized", "rrfb", "island"]
        return gpd.GeoDataFrame(
            {col: pd.Series(dtype="object") for col in columns},
            geometry=gpd.GeoSeries([], dtype="geometry"),
            crs="EPSG:4326",
        )

    leg_counts: Counter[int] = Counter()
    if "start_node_id" in segments_df.columns:
        leg_counts.update(segments_df["start_node_id"].dropna().astype(int))
    if "end_node_id" in segments_df.columns:
        leg_counts.update(segments_df["end_node_id"].dropna().astype(int))
    seg_node_ids = set(leg_counts)

    intersection_nodes = nodes_df[nodes_df["node_id"].isin(seg_node_ids)].copy()
    intersection_nodes["legs"] = intersection_nodes["node_id"].map(leg_counts)

    if "stop" in intersection_nodes.columns and "stops" not in intersection_nodes.columns:
        intersection_nodes = intersection_nodes.rename(columns={"stop": "stops"})

    geoms = gpd.points_from_xy(intersection_nodes[lon_col], intersection_nodes[lat_col])
    return gpd.GeoDataFrame(intersection_nodes, geometry=geoms, crs="EPSG:4326")


def _parse_geom(geom_series: pd.Series) -> tuple[list, str | None]:
    """Parse a geometry column (shapely objects or EWKB-hex strings) + infer CRS.

    Handles shapely geometry objects (live run), EWKB hex strings with an embedded SRID
    (the ``dest_*.parquet`` encoding), and ``None`` / NaN.
    """
    from shapely import wkb as swkb
    from shapely.geometry import Point

    geoms: list = []
    src_crs: str | None = None

    for val in geom_series:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            geoms.append(None)
            continue
        if isinstance(val, str):
            raw = bytes.fromhex(val)
            little = raw[0] == 1
            order = "little" if little else "big"
            type_int = int.from_bytes(raw[1:5], order)
            has_srid = bool(type_int & 0x20000000)
            if has_srid and src_crs is None:
                srid = int.from_bytes(raw[5:9], order)
                src_crs = f"EPSG:{srid}"
            payload = raw[9:] if has_srid else raw
            type_bytes = (type_int & ~0x20000000).to_bytes(4, order)
            geoms.append(swkb.loads(raw[:1] + type_bytes + payload))
        elif isinstance(val, Point):
            geoms.append(val)
        else:
            geoms.append(val)

    return geoms, src_crs


def _build_destination_gdf(
    dest_df: pd.DataFrame,
    blocks_df: gpd.GeoDataFrame | None,
    dest_type: str = "destination",
) -> gpd.GeoDataFrame | None:
    """Convert a destination DataFrame to a Point/Polygon GeoDataFrame in WGS84.

    Per row the active geometry is ``geom_pt`` if set, else ``geom_poly``. The source CRS
    comes from whichever column carries an embedded SRID; failing that, a UTM CRS is
    estimated from ``blocks_df`` (``ValueError`` if ``blocks_df`` is ``None`` then).
    Returns ``None`` for an empty input.
    """
    if dest_df.empty:
        return None

    import geopandas as gpd

    pt_geoms, pt_crs = _parse_geom(dest_df["geom_pt"]) if "geom_pt" in dest_df.columns else ([None] * len(dest_df), None)
    poly_geoms, poly_crs = _parse_geom(dest_df["geom_poly"]) if "geom_poly" in dest_df.columns else ([None] * len(dest_df), None)

    if pt_crs is not None and poly_crs is not None and pt_crs != poly_crs:
        raise ValueError(
            f"destination type {dest_type!r}: geom_pt and geom_poly have differing SRIDs "
            f"({pt_crs} vs {poly_crs})"
        )
    src_crs = pt_crs if pt_crs is not None else poly_crs

    both = sum(1 for p, g in zip(pt_geoms, poly_geoms) if p is not None and g is not None)
    if both:
        _logger.warning(
            "destination type %r: %d row(s) have both geom_pt and geom_poly; using geom_pt",
            dest_type, both,
        )

    geoms = [p if p is not None else g for p, g in zip(pt_geoms, poly_geoms)]

    if src_crs is None:
        if blocks_df is None:
            raise ValueError(
                f"destination type {dest_type!r}: cannot determine source CRS "
                f"(no embedded SRID and no census blocks available)"
            )
        blocks_4326 = (
            blocks_df.to_crs(epsg=4326)
            if blocks_df.crs is not None and blocks_df.crs.to_epsg() != 4326
            else blocks_df
        )
        non_null = blocks_4326.geometry.dropna()
        if non_null.empty:
            return None
        try:
            src_crs = f"EPSG:{non_null.iloc[:1].estimate_utm_crs().to_epsg()}"
        except RuntimeError:
            return None

    data_cols = [c for c in dest_df.columns if c not in ("geom_pt", "geom_poly")]
    gdf = gpd.GeoDataFrame(dest_df[data_cols], geometry=geoms, crs=src_crs)
    return gdf.to_crs(epsg=4326)


def _format_connectivity_csv(connectivity_df: pd.DataFrame) -> pd.DataFrame:
    """Format the connectivity table for CSV: booleans -> ``t``/``f``."""
    df = connectivity_df.copy()
    for col in ("low_stress", "high_stress"):
        if col in df.columns:
            df[col] = df[col].map({True: "t", False: "f"})
    return df


# ── Target registrations ─────────────────────────────────────────────────────
# The output filenames below are the contract with bikescore_bna.stages.*; keep in sync.

# Raw per-stage geo outputs (read straight from the stage's parquet).
_simple_target("ways_raw", "parse", "ways_raw.parquet", "geo")
_simple_target("nodes", "parse", "nodes.parquet", "geo")
_simple_target("census_blocks", "census", "census_blocks.parquet", "geo")
_simple_target("ways_classified", "attributes", "ways_classified.parquet", "geo")
_simple_target("segments", "segment", "segments.parquet", "geo")
_simple_target("trails", "segment", "trails.parquet", "geo")
_simple_target("stress", "stress", "stress.parquet", "geo")
_simple_target("blocks_with_roads", "graph", "blocks_with_roads.parquet", "geo")

# Raw per-stage plain outputs.
_simple_target("jobs", "jobs", "jobs.parquet", "plain")
_simple_target("scores", "scores", "scores.parquet", "plain")
_simple_target("neighborhood_scores", "neighborhood", "neighborhood.parquet", "plain")
_simple_target("score_inputs", "neighborhood", "score_inputs.parquet", "plain")
_simple_target("mileage", "neighborhood", "mileage.parquet", "plain")


@_target("neighborhood_census_blocks", "census", requires=["census", "scores"], kind="geo")
def _build_neighborhood_census_blocks(ctx: ExportContext) -> gpd.GeoDataFrame:
    scores_df = ctx.stage_df("scores", "scores.parquet")
    blocks_df = ctx.stage_df("census", "census_blocks.parquet", geo=True)
    return _build_census_blocks_gdf(scores_df, blocks_df)


@_target("intersections", "parse", requires=["parse", "stress"], kind="geo")
def _build_intersections(ctx: ExportContext) -> gpd.GeoDataFrame:
    nodes_df = ctx.stage_df("parse", "nodes.parquet")
    segments_df = ctx.stage_df("stress", "stress.parquet", geo=True)
    return _build_intersections_gdf(nodes_df, segments_df)


@_target("boundary", "parse", requires=["dataset:boundary"], kind="geo")
def _build_boundary(ctx: ExportContext) -> gpd.GeoDataFrame:
    import geopandas as gpd

    path = ctx.dataset_path("boundary")
    if path is None:
        raise StageOutputNotFoundError("dataset 'boundary' not supplied for this run")
    return _to_wgs84(gpd.read_file(path))


@_target(
    "destinations", "destinations", requires=["destinations"], kind="geo",
    fan_out=True, formats=["geojson", "csv"],
)
def _build_destinations(ctx: ExportContext) -> dict[str, gpd.GeoDataFrame]:
    try:
        blocks_df = ctx.stage_df("census", "census_blocks.parquet", geo=True)
    except StageOutputNotFoundError:
        blocks_df = None

    result: dict[str, gpd.GeoDataFrame] = {}
    for path in sorted(ctx.stage_dir("destinations").glob("dest_*.parquet")):
        dest_type = path.stem[len("dest_"):]
        dest_df = pd.read_parquet(path)
        try:
            gdf = _build_destination_gdf(dest_df, blocks_df, dest_type=dest_type)
        except ValueError as exc:
            _logger.warning("skipping destination type %r: %s", dest_type, exc)
            continue
        if gdf is not None and not gdf.empty:
            result[dest_type] = gdf
    return result


@_target("connectivity", "connectivity", requires=["connectivity"], kind="plain")
def _build_connectivity(ctx: ExportContext) -> pd.DataFrame:
    return _format_connectivity_csv(ctx.stage_df("connectivity", "connectivity.parquet"))


def _build_speed_limit_df(city: CityIdentity, config: BNAConfig) -> pd.DataFrame:
    """Build the ``residential_speed_limit`` table from city FIPS + config speeds."""
    if not city.fips_code:
        return pd.DataFrame(columns=["state_fips_code", "city_fips_code", "state_speed", "city_speed"])
    return pd.DataFrame([{
        "state_fips_code": city.fips_code[:2],
        "city_fips_code": city.fips_code,
        "state_speed": config.city.state_default_speed,
        "city_speed": config.city.default_speed,
    }])


@_target("speed_limits", owner_stage=None, requires=[], kind="plain")
def _build_speed_limits(ctx: ExportContext) -> pd.DataFrame:
    return _build_speed_limit_df(ctx.city, ctx.config)


# ── Bundle: `bna` (brokenspoke-analyzer deliverable parity) ──────────────────

_register_bundle(ExportBundle(
    name="bna",
    targets={
        "neighborhood_census_blocks": "neighborhood_census_blocks",
        "stress": "neighborhood_ways",
        "intersections": "neighborhood_ways_intersections",
        "boundary": "neighborhood_boundary",
        "destinations": "neighborhood_{key}",
        "connectivity": "neighborhood_connected_census_blocks",
        "neighborhood_scores": "neighborhood_overall_scores",
        "score_inputs": "neighborhood_score_inputs",
        "mileage": "mileage",
        "speed_limits": "residential_speed_limit",
    },
    formats={
        "neighborhood_census_blocks": ["geojson", "shapefile"],
        "stress": ["geojson", "shapefile"],
        "intersections": ["geojson"],
        "boundary": ["geojson"],
        "destinations": ["geojson"],
        "connectivity": ["csv"],
        "neighborhood_scores": ["csv"],
        "score_inputs": ["csv"],
        "mileage": ["csv"],
        "speed_limits": ["csv"],
    },
    required={"neighborhood_census_blocks", "stress"},
))
