"""Diagnostics: trace an OSM way through pipeline stages."""
# Verbatim port of bna-core's way-trace debug utility. pandas/geopandas element access
# is dynamically typed; pyright's stubs flag Series/ndarray scalar coercions that are
# correct at runtime. Suppress those categories here rather than litter the port with
# per-line ignores — the module is a display/diagnostics tool, not on the scoring path.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportGeneralTypeIssues=false, reportReturnType=false
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from rich.console import Console

# Key columns shown at way stages (parse / classify), in display order
_WAY_KEY_COLS: list[str] = [
    "osm_id",
    "name",
    "highway",
    "functional_class",
    "one_way",
    "maxspeed",
    "speed_limit",
    "ft_lanes",
    "tf_lanes",
    "width_ft",
    "ft_bike_infra",
    "tf_bike_infra",
    "ft_bike_infra_width",
    "tf_bike_infra_width",
    "ft_park",
    "tf_park",
]

# Columns shown per segment row
_SEG_KEY_COLS: list[str] = ["segment_id", "start_node_id", "end_node_id", "road_id"]

# Stress columns added at the stress stage
_STRESS_KEY_COLS: list[str] = [
    "ft_seg_stress", "tf_seg_stress",
    "ft_int_stress", "tf_int_stress",
]

# Columns that are structural / not useful to display
_SKIP_COLS: frozenset[str] = frozenset({"geometry", "node_ids", "tags"})

_WAY_STAGES: frozenset[str] = frozenset({"parse", "attributes"})


@dataclass
class StageTrace:
    """Result of filtering one pipeline stage for a specific OSM way."""

    stage: str
    status: str  # "present" | "dropped" | "orphaned" | "absent" | "not_run"
    rows: pd.DataFrame | None
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)


def _vals_differ(a: Any, b: Any) -> bool:
    try:
        if pd.isna(a) and pd.isna(b):
            return False
        if pd.isna(a) or pd.isna(b):
            return True
    except (TypeError, ValueError):
        pass
    return a != b


def _compute_diff(
    prev_row: pd.Series, curr_row: pd.Series
) -> dict[str, tuple[Any, Any]]:
    diff: dict[str, tuple[Any, Any]] = {}
    for col in prev_row.index:
        if col not in curr_row.index:
            continue
        old, new = prev_row[col], curr_row[col]
        if _vals_differ(old, new):
            diff[col] = (old, new)
    return diff


# Key columns to show per reference parquet in the reference trace display
_REF_STAGE_COLS: dict[str, list[str]] = {
    "ways_raw": [
        "road_id", "osm_id", "name", "length_m", "functional_class", "one_way",
    ],
    "ways_classified": [
        "road_id", "osm_id", "name",
        "functional_class", "ft_bike_infra", "tf_bike_infra", "one_way",
        "ft_park", "tf_park",
    ],
    "ways_imputed": [
        "road_id", "osm_id", "name",
        "speed_limit", "ft_lanes", "tf_lanes", "width_ft",
    ],
    "ways_stress": [
        "road_id", "ft_seg_stress", "tf_seg_stress", "ft_int_stress", "tf_int_stress",
    ],
}

_REF_STAGE_ORDER: tuple[str, ...] = (
    "ways_raw", "ways_classified", "ways_imputed", "ways_stress"
)


def trace_way_reference(
    id_value: int,
    ref_dir: Path,
    *,
    by_road_id: bool = False,
) -> list[StageTrace]:
    """Trace an OSM way through reference parquets (brokenspoke-analyzer ground truth).

    Stages are named after the reference parquet files (not bna-core stage names):
      ways_raw        — base way attributes (osm_id + road_id key)
      ways_classified — classify-equivalent columns (road_id key)
      ways_imputed    — impute-equivalent columns (road_id key)
      ways_stress     — stress columns (road_id key; column misnamed "osm_id" in file)

    Args:
        id_value: OSM way ID (default) or road_id (when ``by_road_id=True``).
        ref_dir: Path to the reference directory (parent of ``stages/``).
        by_road_id: If True, filter by road_id directly rather than resolving
            osm_id → road_ids. Use when the caller already has a road_id (e.g.
            from a validation key).
    """
    stages_dir = ref_dir / "stages"

    ways_raw_path = stages_dir / "ways_raw.parquet"
    if not ways_raw_path.exists():
        return [
            StageTrace(stage=s, status="not_run", rows=None)
            for s in _REF_STAGE_ORDER
        ]

    ways_raw = pd.read_parquet(ways_raw_path)

    if by_road_id:
        raw_rows = ways_raw[ways_raw["road_id"] == id_value].copy()
    else:
        raw_rows = ways_raw[ways_raw["osm_id"] == id_value].copy()

    if len(raw_rows) == 0:
        return [
            StageTrace(stage=s, status="absent", rows=None)
            for s in _REF_STAGE_ORDER
        ]

    road_ids: set[int] = {int(r) for r in raw_rows["road_id"].dropna()}
    id_bridge_cols = [c for c in ("road_id", "osm_id", "name") if c in raw_rows.columns]
    id_bridge = raw_rows[id_bridge_cols].copy()

    traces: list[StageTrace] = []

    # ── ways_raw ──────────────────────────────────────────────────────────────
    raw_show_cols = [c for c in _REF_STAGE_COLS["ways_raw"] if c in raw_rows.columns]
    traces.append(StageTrace(
        stage="ways_raw",
        status="present",
        rows=raw_rows[raw_show_cols].copy(),
    ))

    # ── ways_classified ───────────────────────────────────────────────────────
    cl_path = stages_dir / "ways_classified.parquet"
    if cl_path.exists():
        cl = pd.read_parquet(cl_path)
        cl_rows = cl[cl["road_id"].isin(road_ids)].copy()
        cl_rows = cl_rows.merge(id_bridge, on="road_id", how="left")
        traces.append(StageTrace(
            stage="ways_classified",
            status="present" if len(cl_rows) > 0 else "absent",
            rows=cl_rows if len(cl_rows) > 0 else None,
        ))
    else:
        traces.append(StageTrace(stage="ways_classified", status="not_run", rows=None))

    # ── ways_imputed ──────────────────────────────────────────────────────────
    imp_path = stages_dir / "ways_imputed.parquet"
    if imp_path.exists():
        imp = pd.read_parquet(imp_path)
        imp_rows = imp[imp["road_id"].isin(road_ids)].copy()
        imp_rows = imp_rows.merge(id_bridge, on="road_id", how="left")
        traces.append(StageTrace(
            stage="ways_imputed",
            status="present" if len(imp_rows) > 0 else "absent",
            rows=imp_rows if len(imp_rows) > 0 else None,
        ))
    else:
        traces.append(StageTrace(stage="ways_imputed", status="not_run", rows=None))

    # ── ways_stress ───────────────────────────────────────────────────────────
    # ways_stress.parquet has one row per OSM way (osm_id key).
    st_path = stages_dir / "ways_stress.parquet"
    if st_path.exists():
        st = pd.read_parquet(st_path)
        osm_id_val = (
            int(raw_rows["osm_id"].iloc[0])
            if by_road_id and "osm_id" in raw_rows.columns and len(raw_rows) > 0
            else id_value
        )
        st_rows = st[st["osm_id"] == osm_id_val].copy()
        traces.append(StageTrace(
            stage="ways_stress",
            status="present" if len(st_rows) > 0 else "absent",
            rows=st_rows if len(st_rows) > 0 else None,
        ))
    else:
        traces.append(StageTrace(stage="ways_stress", status="not_run", rows=None))

    return traces


def print_reference_trace(
    traces: list[StageTrace],
    console: Console,
    id_value: int,
    city_slug: str | None = None,
    *,
    wide: bool = False,
) -> None:
    """Print a reference trace (brokenspoke-analyzer parquets) to the console."""
    from rich.table import Table

    title = f"Way {id_value}"
    if city_slug:
        title += f"  ({city_slug})"
    title += "  [reference]"
    console.print(f"\n[bold]{title}[/bold]")
    console.print("─" * 64)

    for trace in traces:
        stage = trace.stage

        if trace.status == "not_run":
            console.print(f"\n[dim][{stage}]  - not available[/dim]")
            continue

        if trace.status == "absent":
            console.print(f"\n[[bold]{stage}[/bold]]  [red]✗  not found[/red]")
            continue

        rows = trace.rows
        assert rows is not None
        n = len(rows)

        console.print(f"\n[[bold]{stage}[/bold]]  [green]✓[/green]  {n} row{'s' if n != 1 else ''}")

        key_cols = (
            _REF_STAGE_COLS.get(stage, [])
            if not wide
            else [c for c in rows.columns if c not in _SKIP_COLS]
        )
        avail = [c for c in key_cols if c in rows.columns] or [
            c for c in rows.columns if c not in _SKIP_COLS
        ]

        tbl = Table(
            show_header=True,
            header_style="bold",
            box=None,
            pad_edge=False,
            show_edge=False,
        )
        tbl.add_column("#", style="dim", width=4)
        for col in avail:
            tbl.add_column(col)
        for i, (_, row) in enumerate(rows.iterrows()):
            tbl.add_row(str(i), *[_fmt(row[c]) for c in avail])
        console.print(tbl)

    console.print()


# ── Intersection context ──────────────────────────────────────────────────────

@dataclass
class CrossingInfo:
    """One eligible crossing road at an intersection node."""

    road_id: int
    osm_id: int | None
    name: str | None
    functional_class: str | None
    speed_limit: float | None
    ft_lanes: float | None
    tf_lanes: float | None
    one_way: str | None
    is_high: bool
    excluded_same_name: bool


@dataclass
class NodeContext:
    """Node attributes plus all crossing roads at one intersection node."""

    node_id: int
    signalized: bool
    stop: bool
    rrfb: bool
    island: bool
    crossings: list[CrossingInfo]


def get_intersection_context(
    way_id: int,
    results: dict[str, Any],
) -> dict[int, NodeContext]:
    """Return crossing-road context for every node of the way's stress segments.

    Reconstructs the road_at_node join used internally by _compute_crossing_booleans,
    restricted to the unique nodes of the traced way.

    Returns:
        Mapping of node_id → NodeContext. Empty if the way has no stress segments
        or if start/end node columns are absent.
    """
    from bikescore_bna.rules.providers import _CROSSING_ALL, _crossing_high_stress_vec

    if "stress" not in results:
        return {}

    stress_df: pd.DataFrame = results["stress"]
    way_segs = stress_df[stress_df["osm_id"] == way_id]
    if way_segs.empty:
        return {}
    if "start_node_id" not in stress_df.columns or "end_node_id" not in stress_df.columns:
        return {}

    _, nodes_df, _ = results["parse"]

    # Node attribute lookup (node_key="node_id" as used in pipeline)
    _node_index_col = "node_id" if "node_id" in nodes_df.columns else nodes_df.columns[0]
    _node_attr_cols = [c for c in ("signalized", "stops", "rrfb", "island") if c in nodes_df.columns]
    node_attrs = nodes_df.set_index(_node_index_col)[_node_attr_cols] if _node_attr_cols else pd.DataFrame()

    def _safe(col: str) -> pd.Series:
        return stress_df[col] if col in stress_df.columns else pd.Series(pd.NA, index=stress_df.index)

    # Build road_at_node table (mirrors _compute_crossing_booleans)
    adj_fc = stress_df["functional_class"].copy() if "functional_class" in stress_df.columns else pd.Series(pd.NA, index=stress_df.index)
    base = pd.DataFrame({
        "cross_road_id": stress_df["road_id"].values,
        "cross_osm_id":  _safe("osm_id").values,
        "cross_name":    _safe("name").values,
        "cross_fc":      adj_fc.values,
        "cross_one_way": _safe("one_way").values,
        "cross_ft_lanes": _safe("ft_lanes").values,
        "cross_tf_lanes": _safe("tf_lanes").values,
        "cross_speed":   _safe("speed_limit").values,
    }, index=stress_df.index)

    at_start = base.copy()
    at_start["node_id"] = _safe("start_node_id").values
    at_end = base.copy()
    at_end["node_id"] = _safe("end_node_id").values

    road_at_node = pd.concat([at_start, at_end], ignore_index=True)
    road_at_node = road_at_node.drop_duplicates(subset=["cross_road_id", "node_id"])
    road_at_node = road_at_node[road_at_node["cross_fc"].isin(_CROSSING_ALL)]

    # Unique nodes from the traced way
    all_way_road_ids: set[int] = set(way_segs["road_id"].tolist())
    way_name: str | None = way_segs.iloc[0]["name"] if "name" in way_segs.columns else None

    unique_nodes: set[int] = set()
    for col in ("end_node_id", "start_node_id"):
        if col in way_segs.columns:
            unique_nodes |= {int(v) for v in way_segs[col].dropna()}

    out: dict[int, NodeContext] = {}

    for node_id in sorted(unique_nodes):
        # Node attributes
        if not node_attrs.empty and node_id in node_attrs.index:
            nr = node_attrs.loc[node_id]
            sig  = bool(nr.get("signalized", False))
            stop = bool(nr.get("stops", False))
            rrfb = bool(nr.get("rrfb", False))
            isl  = bool(nr.get("island", False))
        else:
            sig = stop = rrfb = isl = False

        # Crossing roads at this node (excluding the traced way's own segments)
        at_node = road_at_node[road_at_node["node_id"] == node_id].copy()
        at_node = at_node[~at_node["cross_road_id"].isin(all_way_road_ids)]

        # Name filter
        same_name_mask = (
            at_node["cross_name"].notna()
            & (way_name is not None)
            & (at_node["cross_name"] == way_name)
        )

        # Attach node-level rrfb / island for high-stress computation
        at_node["rrfb"] = rrfb
        at_node["island"] = isl

        eligible = at_node[~same_name_mask].copy()
        excluded = at_node[same_name_mask].copy()

        if not eligible.empty:
            eligible["is_high"] = _crossing_high_stress_vec(eligible)

        crossings: list[CrossingInfo] = []
        for _, row in eligible.iterrows():
            crossings.append(CrossingInfo(
                road_id=int(row["cross_road_id"]),
                osm_id=int(row["cross_osm_id"]) if pd.notna(row.get("cross_osm_id")) else None,
                name=row.get("cross_name"),
                functional_class=row.get("cross_fc"),
                speed_limit=row.get("cross_speed") if pd.notna(row.get("cross_speed")) else None,
                ft_lanes=row.get("cross_ft_lanes") if pd.notna(row.get("cross_ft_lanes")) else None,
                tf_lanes=row.get("cross_tf_lanes") if pd.notna(row.get("cross_tf_lanes")) else None,
                one_way=row.get("cross_one_way") if pd.notna(row.get("cross_one_way")) else None,
                is_high=bool(row.get("is_high", False)),
                excluded_same_name=False,
            ))
        for _, row in excluded.iterrows():
            crossings.append(CrossingInfo(
                road_id=int(row["cross_road_id"]),
                osm_id=int(row["cross_osm_id"]) if pd.notna(row.get("cross_osm_id")) else None,
                name=row.get("cross_name"),
                functional_class=row.get("cross_fc"),
                speed_limit=row.get("cross_speed") if pd.notna(row.get("cross_speed")) else None,
                ft_lanes=row.get("cross_ft_lanes") if pd.notna(row.get("cross_ft_lanes")) else None,
                tf_lanes=row.get("cross_tf_lanes") if pd.notna(row.get("cross_tf_lanes")) else None,
                one_way=row.get("cross_one_way") if pd.notna(row.get("cross_one_way")) else None,
                is_high=False,
                excluded_same_name=True,
            ))

        out[node_id] = NodeContext(
            node_id=node_id,
            signalized=sig,
            stop=stop,
            rrfb=rrfb,
            island=isl,
            crossings=crossings,
        )

    return out


def _print_intersection_context(
    context: dict[int, NodeContext],
    console: Console,
    way_segs: pd.DataFrame,
) -> None:
    """Print the intersection context section following the stress table."""
    from rich.table import Table

    if not context:
        return

    console.print("\n  [bold]Intersection context[/bold]")

    # Map each node to which segments it belongs to (ft end / tf start)
    node_roles: dict[int, list[str]] = {}
    for i, (_, seg) in enumerate(way_segs.iterrows()):
        if "end_node_id" in seg.index:
            n = int(seg["end_node_id"])
            node_roles.setdefault(n, []).append(f"ft-end of seg #{i}")
        if "start_node_id" in seg.index:
            n = int(seg["start_node_id"])
            node_roles.setdefault(n, []).append(f"tf-start of seg #{i}")

    for node_id, ctx in context.items():
        role_str = "  ·  ".join(node_roles.get(node_id, []))
        attrs = "  ".join([
            f"signalized={'Y' if ctx.signalized else 'N'}",
            f"stop={'Y' if ctx.stop else 'N'}",
            f"rrfb={'Y' if ctx.rrfb else 'N'}",
            f"island={'Y' if ctx.island else 'N'}",
        ])
        console.print(f"\n  node [cyan]{node_id}[/cyan]  ({role_str})")
        console.print(f"  {attrs}")

        if not ctx.crossings:
            console.print("    [dim]no eligible crossing roads[/dim]")
            continue

        tbl = Table(
            show_header=True,
            header_style="bold",
            box=None,
            pad_edge=False,
            show_edge=False,
            padding=(0, 1),
        )
        for col in ("road_id", "osm_id", "name", "fc", "speed", "ft_lanes", "tf_lanes", "one_way", "high?"):
            tbl.add_column(col)

        for c in ctx.crossings:
            high_cell = (
                "[dim]excl. (same name)[/dim]" if c.excluded_same_name
                else ("[bold red]YES[/bold red]" if c.is_high else "[green]no[/green]")
            )
            tbl.add_row(
                str(c.road_id),
                str(c.osm_id) if c.osm_id is not None else "-",
                c.name or "-",
                c.functional_class or "-",
                _fmt(c.speed_limit),
                _fmt(c.ft_lanes),
                _fmt(c.tf_lanes),
                c.one_way or "None",
                high_cell,
            )
        console.print(tbl)


def resolve_osm_id_from_road_id(road_id: int, results: dict[str, Any]) -> int | None:
    """Return the osm_id of the way whose segment has end_node_id == road_id.

    The validation key column is road_id (= end_node_id), not osm_id.
    Use this to convert a validation key back to an osm_id for tracing.
    """
    from bikescore_bna.stages.segment import SegmentResult

    if "segment" in results:
        seg_result: SegmentResult
        seg_result, _ = results["segment"]
        segs = seg_result.segments
        if "road_id" in segs.columns and "osm_id" in segs.columns:
            hit = segs[segs["road_id"] == road_id]
            if len(hit) > 0:
                return int(hit.iloc[0]["osm_id"])

    if "stress" in results:
        segs = results["stress"]
        if "road_id" in segs.columns and "osm_id" in segs.columns:
            hit = segs[segs["road_id"] == road_id]
            if len(hit) > 0:
                return int(hit.iloc[0]["osm_id"])

    return None


def trace_way(way_id: int, results: dict[str, Any]) -> list[StageTrace]:
    """Trace an OSM way through pipeline stage results.

    Args:
        way_id: OSM way ID to trace.
        results: Pipeline._results dict — keys are stage names, values are
            the stage outputs (same structure the pipeline uses internally).

    Returns:
        List of StageTrace objects in pipeline order.
    """
    from bikescore_bna.stages.segment import SegmentResult  # local to avoid circular import

    traces: list[StageTrace] = []
    prev_row: pd.Series | None = None
    seen_any = False  # True once the way appeared in at least one stage

    for stage in ("parse", "attributes", "segment", "stress"):
        if stage not in results:
            traces.append(StageTrace(stage=stage, status="not_run", rows=None))
            continue

        if stage == "parse":
            ways_df: pd.DataFrame = results["parse"][0]
            rows = ways_df[ways_df["osm_id"] == way_id].copy()
        elif stage == "attributes":
            rows = results[stage][results[stage]["osm_id"] == way_id].copy()
        elif stage == "segment":
            seg_result: SegmentResult
            seg_result, _ = results["segment"]
            if way_id in seg_result.orphan_osm_ids:
                traces.append(StageTrace(stage="segment", status="orphaned", rows=None))
                continue
            rows = seg_result.segments[seg_result.segments["osm_id"] == way_id].copy()
        else:  # stress
            segs: pd.DataFrame = results["stress"]
            rows = segs[segs["osm_id"] == way_id].copy()

        if len(rows) == 0:
            status = "dropped" if seen_any else "absent"
            traces.append(StageTrace(stage=stage, status=status, rows=None))
            continue

        seen_any = True

        diff: dict[str, tuple[Any, Any]] = {}
        if stage in _WAY_STAGES and prev_row is not None:
            diff = _compute_diff(prev_row, rows.iloc[0])

        if stage in _WAY_STAGES:
            prev_row = rows.iloc[0]

        traces.append(StageTrace(stage=stage, status="present", rows=rows, diff=diff))

    return traces


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt(v: Any) -> str:
    if v is None:
        return "None"
    try:
        if pd.isna(v):
            return "None"
    except (TypeError, ValueError):
        pass
    return str(v)


def print_trace(
    traces: list[StageTrace],
    console: Console,
    way_id: int,
    city_slug: str | None = None,
    *,
    label: str | None = None,
    wide: bool = False,
    intersection_context: dict[int, NodeContext] | None = None,
) -> None:
    """Print a formatted way trace to the console using rich."""
    from rich.table import Table

    title = f"Way {way_id}"
    if city_slug:
        title += f"  ({city_slug})"
    if label:
        title += f"  [{label}]"
    console.print(f"\n[bold]{title}[/bold]")
    console.print("─" * 64)

    stress_rows: pd.DataFrame | None = None

    for trace in traces:
        stage = trace.stage

        if trace.status == "not_run":
            console.print(f"\n[dim][{stage}]  - not run[/dim]")
            continue

        if trace.status == "orphaned":
            console.print(
                f"\n[[bold]{stage}[/bold]]  [red]✗  DROPPED — orphan"
                " (no shared nodes with other ways)[/red]"
            )
            continue

        if trace.status in ("dropped", "absent"):
            _lbl = (
                "not present in parse output"
                if trace.status == "absent"
                else "not found in output"
            )
            console.print(f"\n[[bold]{stage}[/bold]]  [red]✗  {_lbl}[/red]")
            continue

        rows = trace.rows
        assert rows is not None
        diff = trace.diff
        n = len(rows)

        if stage in _WAY_STAGES:
            diff_note = (
                f"  [yellow]({len(diff)} column{'s' if len(diff) != 1 else ''})[/yellow]"
                if diff
                else ""
            )
            console.print(
                f"\n[[bold]{stage}[/bold]]  [green]✓[/green]  {n} row{diff_note}"
            )
            key_cols = (
                _WAY_KEY_COLS
                if not wide
                else [c for c in rows.columns if c not in _SKIP_COLS]
            )
            row = rows.iloc[0]
            for col in key_cols:
                if col not in row.index:
                    continue
                val = row[col]
                if col in diff:
                    old_val, _ = diff[col]
                    console.print(
                        f"  [cyan]{col:<28}[/cyan] {_fmt(val)}"
                        f"  [dim](was: {_fmt(old_val)})[/dim]"
                    )
                else:
                    console.print(f"  [cyan]{col:<28}[/cyan] {_fmt(val)}")

        else:  # segment / stress
            console.print(
                f"\n[[bold]{stage}[/bold]]  [green]✓[/green]"
                f"  {n} segment{'s' if n != 1 else ''}"
            )
            if stage == "segment":
                show_cols = (
                    _SEG_KEY_COLS
                    if not wide
                    else [c for c in rows.columns if c not in _SKIP_COLS | {"osm_id"}]
                )
            else:
                show_cols = (
                    _SEG_KEY_COLS + _STRESS_KEY_COLS
                    if not wide
                    else [c for c in rows.columns if c not in _SKIP_COLS | {"osm_id"}]
                )
                stress_rows = rows

            avail = [c for c in show_cols if c in rows.columns]
            tbl = Table(
                show_header=True,
                header_style="bold",
                box=None,
                pad_edge=False,
                show_edge=False,
            )
            tbl.add_column("#", style="dim", width=4)
            for col in avail:
                tbl.add_column(col)
            for i, (_, row) in enumerate(rows.iterrows()):
                tbl.add_row(str(i), *[_fmt(row[c]) for c in avail])
            console.print(tbl)

    if intersection_context and stress_rows is not None:
        _print_intersection_context(intersection_context, console, stress_rows)

    console.print()
