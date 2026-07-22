"""Validation framework — column-level comparison against brokenspoke-analyzer reference."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bikescore_bna.deviations import KnownDeviation

_MAX_DETAIL_ROWS = 50  # max differing rows stored for full-detail report
_MAX_EXAMPLES = 5      # examples shown in column-level summary
_DEVIATION_SAMPLE = 3  # expected rows shown in detail section


@dataclass
class ColumnDiff:
    """Differences for a single column between Python output and reference."""

    column: str
    n_total: int    # rows matched by key (inner join)
    n_differ: int
    max_diff: float | None  # None for non-numeric columns
    mean_diff: float | None
    examples: list[dict[str, Any]] = field(default_factory=list)
    """Up to _MAX_EXAMPLES rows showing the difference (key, computed, reference)."""

    # Absolute diff percentiles (None for non-numeric or zero diffs)
    p50_diff: float | None = None
    p75_diff: float | None = None
    p90_diff: float | None = None
    p95_diff: float | None = None
    # Directional split: how many rows have computed > reference vs computed < reference
    n_positive: int = 0  # computed > reference
    n_negative: int = 0  # computed < reference
    # Relative diff |computed - reference| / |reference|
    max_rel_diff: float | None = None   # max across differing rows
    p90_rel_diff: float | None = None   # 90th percentile

    @property
    def passed(self) -> bool:
        """True when no differing values were found for this column."""
        return self.n_differ == 0


@dataclass
class RowDiff:
    """All column-level differences for one row, with optional upstream context."""

    key: Any  # The key value (e.g. osm_id)
    col_diffs: dict[str, tuple[Any, Any]]  # column → (computed_value, reference_value)
    computed_row: dict[str, Any] = field(default_factory=dict)   # full computed row
    reference_row: dict[str, Any] = field(default_factory=dict)  # full reference row
    context: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Keyed by stage name (e.g. 'attributes'), maps column → value for upstream inputs."""
    notes: list[str] = field(default_factory=list)
    """Free-text diagnostic notes added by the caller (e.g. boundary check result)."""
    expected_deviations: dict[str, str] = field(default_factory=dict)
    """column → deviation name for each diff explained by a known SQL bug fix."""
    shadow_confirmed_columns: set[str] = field(default_factory=set)
    """Columns confirmed via shadow computation (subset of expected_deviations keys)."""


@dataclass
class ValidationReport:
    """Result of comparing a stage output against the brokenspoke-analyzer reference."""

    stage: str
    city: str

    # ── join coverage ──────────────────────────────────────────────────────────
    n_computed: int       # rows in computed before join
    n_reference: int      # rows in reference before join
    rows_total: int       # rows matched by inner join (= n_matched)
    rows_differing: int   # rows from inner join with ≥1 column diff
    rows_only_computed: int = 0          # keys in computed but not reference
    rows_only_reference: int = 0         # keys in reference but not computed
    keys_only_computed: list[Any] = field(default_factory=list)   # up to 20 examples
    keys_only_reference: list[Any] = field(default_factory=list)  # up to 20 examples

    # ── column-level summary ───────────────────────────────────────────────────
    column_diffs: dict[str, ColumnDiff] = field(default_factory=dict)

    # ── row-level detail ───────────────────────────────────────────────────────
    row_diffs: list[RowDiff] = field(default_factory=list)
    """Per-row detail for up to _MAX_DETAIL_ROWS differing rows."""

    wall_time_seconds: float = 0.0
    report_notes: list[str] = field(default_factory=list)
    """Report-level diagnostic notes (e.g. boundary position summaries, stage-level context)."""

    # ── known deviation accounting ─────────────────────────────────────────────
    deviation_explained_rows: int = 0
    """Rows where every column diff is explained by a known SQL bug fix."""
    deviation_counts: dict[str, int] = field(default_factory=dict)
    """Per-deviation name: number of (row, column) pairs matched across ALL differing rows."""
    deviation_shadow_confirmed_rows: int = 0
    """Rows where every column diff was shadow-confirmed (exact SQL replica match)."""
    deviation_shadow_counts: dict[str, int] = field(default_factory=dict)
    """Per-deviation name: shadow-confirmed (row, column) pair count."""

    # ── known absence deviation accounting ────────────────────────────────────
    rows_only_reference_expected: int = 0
    """Reference-only rows explained by a KnownAbsenceDeviation (not counted as failures)."""
    keys_only_reference_expected: list[Any] = field(default_factory=list)
    """Keys of reference-only rows explained by a KnownAbsenceDeviation (up to 20)."""
    absence_deviation_name: str = ""
    """Name of the KnownAbsenceDeviation that explains rows_only_reference_expected."""

    @property
    def passed(self) -> bool:
        """True when all differences are within tolerance or declared deviations."""
        unexplained_col_diffs = self.rows_differing - self.deviation_explained_rows
        unexplained_ref_only = self.rows_only_reference - self.rows_only_reference_expected
        return (
            unexplained_col_diffs == 0
            and self.rows_only_computed == 0
            and unexplained_ref_only == 0
        )

    # ── console output ─────────────────────────────────────────────────────────

    def print(self) -> None:
        """Print a human-readable summary."""
        if self.stage == "source/files":
            self._print_source_files()
            return
        if self.stage.startswith("source/"):
            self._print_source_data()
            return
        status = "✓ PASSED" if self.passed else "✗ FAILED"
        print(f"\n{status} — {self.stage} ({self.city})")
        print(f"  computed: {self.n_computed:,} rows  |  reference: {self.n_reference:,} rows  "
              f"|  matched: {self.rows_total:,}  ({self.wall_time_seconds:.1f}s)")

        if self.rows_only_computed:
            ex = self.keys_only_computed[:5]
            print(f"  ✗ {self.rows_only_computed:,} rows only in computed (not in reference): {ex}")
        unexpected_ref_only = self.rows_only_reference - self.rows_only_reference_expected
        if unexpected_ref_only > 0:
            ex = [k for k in self.keys_only_reference if k not in self.keys_only_reference_expected][:5]
            print(f"  ✗ {unexpected_ref_only:,} rows only in reference (not in computed): {ex}")
        if self.rows_only_reference_expected > 0:
            ex = self.keys_only_reference_expected[:5]
            dev = f" — {self.absence_deviation_name}" if self.absence_deviation_name else ""
            print(f"  ✓ {self.rows_only_reference_expected:,} rows only in reference (expected{dev}): {ex}")

        if self.column_diffs:
            print(f"\n  {'Column':<35} {'differ':>8} {'max_diff':>12}")
            print(f"  {'-'*57}")
            for col, diff in sorted(self.column_diffs.items()):
                max_s = f"{diff.max_diff:.6f}" if diff.max_diff is not None else "n/a"
                mark = "  " if diff.passed else "✗ "
                print(f"  {mark}{col:<33} {diff.n_differ:>8,} {max_s:>12}")

        # ── Extended diff distribution for numeric columns with differences ───
        numeric_diffs = [
            (col, d) for col, d in sorted(self.column_diffs.items())
            if not d.passed and d.p50_diff is not None
        ]
        if numeric_diffs:
            print("\n  Diff distribution (non-zero diffs, computed → reference):")
            for col, d in numeric_diffs:
                dir_s = f"+{d.n_positive:,} / -{d.n_negative:,}"
                rel_s = ""
                if d.max_rel_diff is not None:
                    rel_s = f"  rel: p90={d.p90_rel_diff*100:.1f}% max={d.max_rel_diff*100:.1f}%"
                print(
                    f"    {col:<28}  "
                    f"p50={d.p50_diff:.0f}  p75={d.p75_diff:.0f}  "
                    f"p90={d.p90_diff:.0f}  p95={d.p95_diff:.0f}  "
                    f"max={d.max_diff:.0f}  ({dir_s}){rel_s}"
                )

        if self.deviation_explained_rows or self.deviation_counts:
            unexplained = self.rows_differing - self.deviation_explained_rows
            print(f"\n  Known SQL bug fixes account for {self.deviation_explained_rows:,} / "
                  f"{self.rows_differing:,} differing rows "
                  f"({unexplained:,} unexpected)")
            shadow_total = sum(self.deviation_shadow_counts.values())
            posthoc_total = sum(self.deviation_counts.values()) - shadow_total
            if shadow_total or posthoc_total:
                print(f"  Match method: {shadow_total} shadow-confirmed, {posthoc_total} post-hoc")
            for dev_name, count in sorted(self.deviation_counts.items()):
                sc = self.deviation_shadow_counts.get(dev_name, 0)
                shadow_note = f" ({sc} shadow-confirmed)" if sc else ""
                print(f"    ↳ {dev_name}: {count} row-column matches{shadow_note}")

        if self.row_diffs:
            unexpected = [rd for rd in self.row_diffs
                          if not (rd.expected_deviations and
                                  set(rd.expected_deviations) >= set(rd.col_diffs))]
            n_unexpected = self.rows_differing - self.deviation_explained_rows
            shown = unexpected[:10]
            if shown:
                print(f"\n  Unexpected rows ({n_unexpected:,} total, {len(shown)} shown):")
                for rd in shown:
                    diffs_str = ", ".join(
                        f"{c}: {v[0]} → {v[1]}" for c, v in rd.col_diffs.items()
                    )
                    print(f"    key={rd.key}  {diffs_str}")
            skipped = self.deviation_explained_rows
            if skipped:
                print(f"  ({skipped:,} expected differences not shown — known SQL bug fixes)")

        if self.report_notes:
            print()
            for note in self.report_notes:
                print(f"  [i] {note}")

    def _print_source_files(self) -> None:
        """Per-file status table for the source/files report."""
        status = "✓ PASSED" if self.passed else "✗ FAILED"
        print(f"\n── Source Data: Files ({self.city})  {status}")
        print()
        for rd in self.row_diffs:
            bna_row = rd.computed_row
            ref_row = rd.reference_row
            if rd.col_diffs:
                col, (bna_val, ref_val) = next(iter(rd.col_diffs.items()))
                print(f"  {rd.key:<16}  ✗ DIFFER  ({col})")
                bna_detail = bna_row.get("detail", "")
                ref_detail = ref_row.get("detail", "")
                print(f"  {'':16}    bna: {bna_val}  {bna_detail}")
                print(f"  {'':16}    ref: {ref_val}  {ref_detail}")
                for note in rd.notes:
                    print(f"  {'':16}    ! {note}")
            else:
                bna_val = bna_row.get("sha256[:12]") or bna_row.get("year", "")
                print(f"  {rd.key:<16}  ✓ MATCH   {bna_val}")
                for note in rd.notes:
                    tiger_bna = bna_row.get("tiger_product", "")
                    tiger_ref = ref_row.get("tiger_product", "")
                    if tiger_bna:
                        print(f"  {'':16}    (hash skipped — bna: {tiger_bna}, ref: {tiger_ref}; same 2020 vintage)")
                    else:
                        short = note[:100] + "…" if len(note) > 100 else note
                        print(f"  {'':16}    ({short})")
                    break
            print()

    def _print_source_data(self) -> None:
        """Coverage + column-diff summary for source/osm, source/census, source/lodes."""
        _LABELS = {
            "source/osm": "OSM Roads",
            "source/census": "Census Blocks",
            "source/lodes": "LODES Jobs",
            "source/boundary": "City Boundary",
        }
        label = _LABELS.get(self.stage, self.stage.replace("source/", "").title())
        status = "✓ PASSED" if self.passed else "✗ FAILED"
        print(f"\n── Source Data: {label} ({self.city})  {status}")
        print(f"   Coverage:  bna {self.n_computed:,}  |  ref {self.n_reference:,}  |  matched {self.rows_total:,}")
        if self.rows_only_computed:
            ex = self.keys_only_computed[:3]
            print(f"              + {self.rows_only_computed:,} only in bna (extra)     e.g. {ex}")
        if self.rows_only_reference:
            ex = self.keys_only_reference[:3]
            print(f"              - {self.rows_only_reference:,} missing from bna        e.g. {ex}")
        if self.column_diffs:
            print(f"\n   {'Column':<35} {'differ':>8} {'max_diff':>12}")
            print(f"   {'─' * 57}")
            for col, diff in sorted(self.column_diffs.items()):
                max_s = f"{diff.max_diff:.6f}" if diff.max_diff is not None else "n/a"
                mark = "✓ " if diff.passed else "✗ "
                print(f"   {mark}{col:<33} {diff.n_differ:>8,} {max_s:>12}")
        if self.row_diffs:
            shown = min(len(self.row_diffs), 10)
            print(f"\n   Sample differing rows ({shown} shown):")
            for rd in self.row_diffs[:10]:
                diffs_str = ", ".join(
                    f"{c}: {v[0]!r} → {v[1]!r}" for c, v in rd.col_diffs.items()
                )
                print(f"     {rd.key}  {diffs_str}")
        if self.report_notes:
            print()
            for note in self.report_notes:
                print(f"   [i] {note}")

    # ── serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise the report to a JSON-compatible dict."""
        return {
            "stage": self.stage,
            "city": self.city,
            "passed": self.passed,
            "n_computed": self.n_computed,
            "n_reference": self.n_reference,
            "rows_total": self.rows_total,
            "rows_differing": self.rows_differing,
            "rows_only_computed": self.rows_only_computed,
            "rows_only_reference": self.rows_only_reference,
            "wall_time_seconds": self.wall_time_seconds,
            "columns": {
                col: {
                    "n_differ": d.n_differ,
                    "max_diff": d.max_diff,
                    "passed": d.passed,
                }
                for col, d in self.column_diffs.items()
            },
            "deviation_explained_rows": self.deviation_explained_rows,
            "deviation_counts": self.deviation_counts,
            "deviation_shadow_confirmed_rows": self.deviation_shadow_confirmed_rows,
            "deviation_shadow_counts": self.deviation_shadow_counts,
            "rows_only_reference_expected": self.rows_only_reference_expected,
            "absence_deviation_name": self.absence_deviation_name,
        }

    # ── markdown report ────────────────────────────────────────────────────────

    def format_markdown(self) -> str:
        """Render a comprehensive Markdown report."""
        lines: list[str] = []
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = "PASSED ✓" if self.passed else "FAILED ✗"

        lines.append(f"# Validation Report — {self.stage} / {self.city}")
        lines.append("")
        lines.append(f"**Generated:** {ts}  ")
        lines.append(f"**Result:** **{status}**  ")
        lines.append(f"**Wall time:** {self.wall_time_seconds:.2f}s")
        lines.append("")

        # ── Row coverage ──────────────────────────────────────────────────────
        lines.append("## Row Coverage")
        lines.append("")
        lines.append("| | Count |")
        lines.append("|---|---:|")
        lines.append(f"| Rows in computed | {self.n_computed:,} |")
        lines.append(f"| Rows in reference | {self.n_reference:,} |")
        lines.append(f"| Matched by key (inner join) | {self.rows_total:,} |")
        lines.append(f"| Only in computed (missing from reference) | {self.rows_only_computed:,} |")
        lines.append(f"| Only in reference (missing from computed) | {self.rows_only_reference:,} |")
        if self.rows_only_reference_expected:
            dev = f" ({self.absence_deviation_name})" if self.absence_deviation_name else ""
            lines.append(f"| Only in reference — expected{dev} | {self.rows_only_reference_expected:,} |")
            unexplained_ref = self.rows_only_reference - self.rows_only_reference_expected
            if unexplained_ref:
                lines.append(f"| Only in reference — unexpected | {unexplained_ref:,} |")
        lines.append(f"| With value differences | {self.rows_differing:,} |")
        if self.deviation_explained_rows:
            unexplained = self.rows_differing - self.deviation_explained_rows
            lines.append(f"| Explained by known SQL bug fixes | {self.deviation_explained_rows:,} |")
            lines.append(f"| Unexpected differences | {unexplained:,} |")
        lines.append("")

        if self.keys_only_computed:
            keys_s = ", ".join(str(k) for k in self.keys_only_computed[:20])
            lines.append(f"**Keys only in computed (up to 20):** {keys_s}")
            lines.append("")
        if self.keys_only_reference:
            unexpected_ref_keys = [k for k in self.keys_only_reference if k not in self.keys_only_reference_expected]
            if unexpected_ref_keys:
                keys_s = ", ".join(str(k) for k in unexpected_ref_keys[:20])
                lines.append(f"**Keys only in reference — unexpected (up to 20):** {keys_s}")
                lines.append("")
            if self.keys_only_reference_expected:
                keys_s = ", ".join(str(k) for k in self.keys_only_reference_expected[:20])
                dev = f" (`{self.absence_deviation_name}`)" if self.absence_deviation_name else ""
                lines.append(f"**Keys only in reference — expected{dev} (up to 20):** {keys_s}")
                lines.append("")

        # ── Column summary ────────────────────────────────────────────────────
        if self.column_diffs:
            lines.append("## Column Summary")
            lines.append("")
            lines.append("| Column | Matched rows | Differ | Max Δ | Mean Δ |")
            lines.append("|---|---:|---:|---:|---:|")
            for col, d in sorted(self.column_diffs.items()):
                mark = "✗ " if not d.passed else ""
                max_s = f"{d.max_diff:.4f}" if d.max_diff is not None else "—"
                mean_s = f"{d.mean_diff:.4f}" if d.mean_diff is not None else "—"
                lines.append(f"| {mark}`{col}` | {d.n_total:,} | {d.n_differ:,} | {max_s} | {mean_s} |")
            lines.append("")

        # ── Known deviations ──────────────────────────────────────────────────
        if self.deviation_counts:
            from bikescore_bna.deviations import KNOWN_DEVIATIONS
            dev_map = {d.name: d for d in KNOWN_DEVIATIONS}
            shadow_total = sum(self.deviation_shadow_counts.values())
            posthoc_total = sum(self.deviation_counts.values()) - shadow_total
            lines.append("## Known SQL Bug Fixes")
            lines.append("")
            lines.append("These differences are intentional: bna-core fixes the SQL bug.")
            if shadow_total or posthoc_total:
                lines.append(f"Match method: {shadow_total} shadow-confirmed (exact), "
                              f"{posthoc_total} post-hoc (heuristic).")
            lines.append("")
            for dev_name, count in sorted(self.deviation_counts.items()):
                dev = dev_map.get(dev_name)
                sc = self.deviation_shadow_counts.get(dev_name, 0)
                shadow_note = f", {sc} shadow-confirmed" if sc else ""
                lines.append(f"### `{dev_name}` ({count} row-column matches{shadow_note})")
                lines.append("")
                if dev:
                    lines.append(dev.description)
                    lines.append("")
                    lines.append(f"*SQL reference: {dev.sql_ref}*")
                    lines.append("")

        # ── Per-row detail ────────────────────────────────────────────────────
        all_diff_rows = [rd for rd in self.row_diffs if rd.col_diffs]
        info_rows = [rd for rd in self.row_diffs if not rd.col_diffs]

        def _is_fully_expected(rd: RowDiff) -> bool:
            return bool(rd.expected_deviations) and set(rd.expected_deviations) >= set(rd.col_diffs)

        unexpected_rows = [rd for rd in all_diff_rows if not _is_fully_expected(rd)]
        expected_sample = [rd for rd in all_diff_rows if _is_fully_expected(rd)][:_DEVIATION_SAMPLE]

        def _render_diff_row(rd: RowDiff, lines: list[str]) -> None:
            lines.append("| Column | Computed | Reference | Δ | Note |")
            lines.append("|---|---|---|---:|---|")
            for col, (comp, ref) in rd.col_diffs.items():
                try:
                    delta = f"{float(comp) - float(ref):+.4f}"
                except (TypeError, ValueError):
                    delta = "—"
                if col in rd.expected_deviations:
                    method = " (shadow)" if col in rd.shadow_confirmed_columns else " (post-hoc)"
                    dev_note = f"expected: `{rd.expected_deviations[col]}`{method}"
                else:
                    dev_note = ""
                lines.append(f"| `{col}` | `{comp}` | `{ref}` | {delta} | {dev_note} |")
            lines.append("")
            if rd.computed_row:
                lines.append("<details><summary>Full computed row</summary>")
                lines.append("")
                lines.append("| Column | Value |")
                lines.append("|---|---|")
                for k, v in rd.computed_row.items():
                    lines.append(f"| `{k}` | `{v}` |")
                lines.append("")
                lines.append("</details>")
                lines.append("")
            if rd.reference_row:
                lines.append("<details><summary>Full reference row</summary>")
                lines.append("")
                lines.append("| Column | Value |")
                lines.append("|---|---|")
                for k, v in rd.reference_row.items():
                    lines.append(f"| `{k}` | `{v}` |")
                lines.append("")
                lines.append("</details>")
                lines.append("")
            for ctx_name, ctx_data in rd.context.items():
                lines.append(f"**Upstream context — {ctx_name}:**")
                lines.append("")
                lines.append("| Column | Value |")
                lines.append("|---|---|")
                for k, v in ctx_data.items():
                    lines.append(f"| `{k}` | `{v}` |")
                lines.append("")
            for note in rd.notes:
                lines.append(f"> {note}")
                lines.append("")

        if unexpected_rows:
            n_unexpected = self.rows_differing - self.deviation_explained_rows
            lines.append(f"## Unexpected Differences ({n_unexpected:,} total, "
                         f"{len(unexpected_rows)} shown)")
            lines.append("")
            for rd in unexpected_rows:
                lines.append(f"### Key: `{rd.key}`")
                lines.append("")
                _render_diff_row(rd, lines)

        if expected_sample:
            n_expected = self.deviation_explained_rows
            lines.append(f"## Expected Differences — Known SQL Bug Fixes "
                         f"({n_expected:,} total, {len(expected_sample)} shown)")
            lines.append("")
            for rd in expected_sample:
                lines.append(f"### Key: `{rd.key}`")
                lines.append("")
                _render_diff_row(rd, lines)

        if info_rows:
            lines.append("## File Notes")
            lines.append("")
            for rd in info_rows:
                lines.append(f"### `{rd.key}`")
                lines.append("")
                if rd.computed_row or rd.reference_row:
                    lines.append("| Field | bna-core | Reference |")
                    lines.append("|---|---|---|")
                    for k in rd.computed_row:
                        bna_v = rd.computed_row.get(k, "—")
                        ref_v = rd.reference_row.get(k, "—")
                        lines.append(f"| `{k}` | `{bna_v}` | `{ref_v}` |")
                    lines.append("")
                for note in rd.notes:
                    lines.append(f"> {note}")
                    lines.append("")

        if self.report_notes:
            lines.append("## Notes")
            lines.append("")
            for note in self.report_notes:
                lines.append(f"> {note}")
                lines.append("")

        return "\n".join(lines)


class Reference:
    """Loads brokenspoke-analyzer reference parquets for a city.

    Reference parquets are exported from brokenspoke-analyzer via:
        uv run python tools/export_reference.py {city} --all --out tests/reference/
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._cache: dict[str, pd.DataFrame] = {}

    def load(self, filename: str) -> pd.DataFrame:
        """Load a reference parquet, caching the result."""
        if filename not in self._cache:
            parquet_path = self.path / "stages" / filename
            if not parquet_path.exists():
                raise FileNotFoundError(
                    f"Reference parquet not found: {parquet_path}\n"
                    f"Generate with: uv run python tools/export_reference.py "
                    f"--stage {filename.replace('.parquet', '')} --out tests/reference/"
                )
            self._cache[filename] = pd.read_parquet(parquet_path)
        return self._cache[filename]


def _scalar(v: Any) -> Any:
    """Convert numpy/pandas scalars to native Python types for clean display.

    If v is a Series or array (e.g. from a non-unique index loc lookup), the
    first element is used.
    """
    if isinstance(v, pd.Series):
        v = v.iloc[0] if len(v) > 0 else None
    if v is pd.NA:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        fv = float(v)
        return None if np.isnan(fv) else fv
    return v


def compare_dataframes(
    computed: pd.DataFrame,
    reference: pd.DataFrame,
    stage: str,
    city: str,
    key_col: str = "road_id",
    columns: list[str] | None = None,
    tolerance: float = 0.0,
    computed_full: pd.DataFrame | None = None,
    deviations: list[KnownDeviation] | None = None,
) -> ValidationReport:
    """Compare a computed DataFrame against reference, column by column.

    Performs an outer join so that rows missing from either side are reported.

    Args:
        computed: Output from the bna-core stage.
        reference: Reference parquet loaded via Reference.load().
        stage: Stage name for reporting.
        city: City slug for reporting.
        key_col: Column to align rows on (default "road_id").
        columns: Columns to validate. None = all columns present in both.
        tolerance: Numeric differences smaller than this are ignored (default 0 = exact).
        computed_full: Optional full computed DataFrame (all columns) for richer row detail.
                       When provided, full rows are embedded in RowDiff objects.
        deviations: Known SQL bug fixes to annotate against. Matching rows are labelled
                    expected in RowDiff.expected_deviations and counted in the report.
    """
    t0 = time.monotonic()

    n_computed = len(computed)
    n_reference = len(reference)

    has_key = key_col in computed.columns and key_col in reference.columns

    if has_key:
        comp_idx = computed.set_index(key_col)
        ref_idx = reference.set_index(key_col)
        merged = comp_idx.join(ref_idx, how="outer", lsuffix="_computed", rsuffix="_ref")
    else:
        comp_idx = ref_idx = None
        merged = computed.join(reference, how="outer", lsuffix="_computed", rsuffix="_ref")

    # ── Identify coverage gaps ─────────────────────────────────────────────────
    if has_key:
        comp_keys = set(computed[key_col].dropna())
        ref_keys = set(reference[key_col].dropna())
        only_computed_keys = sorted(comp_keys - ref_keys)
        only_reference_keys = sorted(ref_keys - comp_keys)
    else:
        only_computed_keys = []
        only_reference_keys = []

    rows_only_computed = len(only_computed_keys)
    rows_only_reference = len(only_reference_keys)

    # Inner-join portion only for column value comparison
    if has_key:
        inner = comp_idx.join(ref_idx, how="inner", lsuffix="_computed", rsuffix="_ref")
    else:
        inner = merged  # fallback

    n_total = len(inner)
    cols_to_check = columns or [
        c for c in reference.columns
        if c != key_col and c in computed.columns
    ]

    column_diffs: dict[str, ColumnDiff] = {}
    rows_differ_set: set = set()

    for col in cols_to_check:
        c_col = f"{col}_computed" if f"{col}_computed" in inner.columns else col
        r_col = f"{col}_ref" if f"{col}_ref" in inner.columns else col
        if c_col not in inner.columns or r_col not in inner.columns:
            continue

        c_vals = inner[c_col]
        r_vals = inner[r_col]

        both_null = c_vals.isna() & r_vals.isna()
        both_valid = ~c_vals.isna() & ~r_vals.isna()

        max_diff = mean_diff = None
        p50_diff = p75_diff = p90_diff = p95_diff = None
        n_positive = n_negative = 0
        max_rel_diff = p90_rel_diff = None
        is_bool = pd.api.types.is_bool_dtype(c_vals) or pd.api.types.is_bool_dtype(r_vals)
        if not is_bool and pd.api.types.is_numeric_dtype(c_vals) and pd.api.types.is_numeric_dtype(r_vals):
            signed_diff = c_vals - r_vals
            diff = signed_diff.abs()
            if both_valid.any():
                max_diff = float(diff[both_valid].max())
                mean_diff = float(diff[both_valid].mean())
            differ_mask = (
                (~both_null & ~both_valid)
                | (both_valid & (diff > tolerance))
            )
            # Compute distribution stats on the differing subset
            nz_mask = differ_mask & both_valid
            if nz_mask.any():
                nz_diff = diff[nz_mask]
                p50_diff = float(nz_diff.quantile(0.50))
                p75_diff = float(nz_diff.quantile(0.75))
                p90_diff = float(nz_diff.quantile(0.90))
                p95_diff = float(nz_diff.quantile(0.95))
                nz_signed = signed_diff[nz_mask]
                n_positive = int((nz_signed > 0).sum())
                n_negative = int((nz_signed < 0).sum())
                # Relative diff: |diff| / |reference|; skip zero-reference rows
                ref_nonzero = r_vals[nz_mask] != 0
                if ref_nonzero.any():
                    rel = (nz_diff[ref_nonzero] / r_vals[nz_mask][ref_nonzero].abs())
                    max_rel_diff = float(rel.max())
                    p90_rel_diff = float(rel.quantile(0.90))
        else:
            differ_mask = (
                (~both_null & ~both_valid)
                | (both_valid & (c_vals.astype(str) != r_vals.astype(str)))
            )

        n_differ = int(differ_mask.sum())

        examples: list[dict] = []
        if n_differ > 0:
            for idx in inner[differ_mask].head(_MAX_EXAMPLES).index:
                examples.append({
                    "key": idx,
                    "computed": _scalar(inner.loc[idx, c_col]),
                    "reference": _scalar(inner.loc[idx, r_col]),
                })
            rows_differ_set.update(inner[differ_mask].index)

        column_diffs[col] = ColumnDiff(
            column=col,
            n_total=n_total,
            n_differ=n_differ,
            max_diff=max_diff,
            mean_diff=mean_diff,
            examples=examples,
            p50_diff=p50_diff,
            p75_diff=p75_diff,
            p90_diff=p90_diff,
            p95_diff=p95_diff,
            n_positive=n_positive,
            n_negative=n_negative,
            max_rel_diff=max_rel_diff,
            p90_rel_diff=p90_rel_diff,
        )

    # ── Build per-row diffs ────────────────────────────────────────────────────
    row_diffs: list[RowDiff] = []

    # Build lookup for full computed rows (use computed_full if provided)
    _full_computed = computed_full if computed_full is not None else computed
    if has_key and key_col in _full_computed.columns:
        full_comp_idx = _full_computed.set_index(key_col)
    else:
        full_comp_idx = _full_computed

    # ── Deviation annotation (all differing rows, not just detail subset) ──────
    relevant_devs = [
        d for d in (deviations or [])
        if stage.startswith(d.stage)
    ]
    row_deviation_map: dict[Any, dict[str, str]] = {}
    row_shadow_confirmed_map: dict[Any, set[str]] = {}
    deviation_counts: dict[str, int] = {}
    deviation_shadow_counts: dict[str, int] = {}
    deviation_explained_rows = 0
    deviation_shadow_confirmed_rows = 0
    fully_explained_keys: set[Any] = set()

    if relevant_devs:
        for dev_key in rows_differ_set:
            if dev_key not in inner.index:
                continue
            comp_row_dev: dict[str, Any] = {}
            if dev_key in full_comp_idx.index:
                raw = full_comp_idx.loc[dev_key]
                if isinstance(raw, pd.DataFrame):
                    raw = raw.iloc[0]
                comp_row_dev = {
                    k: _scalar(v) for k, v in raw.items()
                    if not hasattr(v, "geom_type")
                }
            col_explanations: dict[str, str] = {}
            shadow_confirmed_cols: set[str] = set()
            differing_cols: set[str] = set()
            for col in cols_to_check:
                c_col = f"{col}_computed" if f"{col}_computed" in inner.columns else col
                r_col = f"{col}_ref" if f"{col}_ref" in inner.columns else col
                if c_col not in inner.columns or r_col not in inner.columns:
                    continue
                cv = _scalar(inner.loc[dev_key, c_col])
                rv = _scalar(inner.loc[dev_key, r_col])
                if cv is None and rv is None:
                    continue
                if cv == rv:
                    continue
                differing_cols.add(col)
                for dev in relevant_devs:
                    if col not in dev.columns:
                        continue
                    # Shadow check: exact — shadow col value == reference value
                    shadow_col = dev.shadow_columns.get(col)
                    _sv = comp_row_dev.get(shadow_col) if shadow_col else None
                    # Normalise NaN → None so float(nan)==None comparison works
                    shadow_val = None if (isinstance(_sv, float) and np.isnan(_sv)) else _sv
                    if shadow_col and shadow_val == rv:
                        col_explanations[col] = dev.name
                        deviation_counts[dev.name] = deviation_counts.get(dev.name, 0) + 1
                        deviation_shadow_counts[dev.name] = deviation_shadow_counts.get(dev.name, 0) + 1
                        shadow_confirmed_cols.add(col)
                        break
                    # Post-hoc fallback
                    if dev.match(col, cv, rv, comp_row_dev):
                        col_explanations[col] = dev.name
                        deviation_counts[dev.name] = deviation_counts.get(dev.name, 0) + 1
                        break
            if col_explanations:
                row_deviation_map[dev_key] = col_explanations
            if shadow_confirmed_cols:
                row_shadow_confirmed_map[dev_key] = shadow_confirmed_cols
            if differing_cols and differing_cols <= set(col_explanations.keys()):
                deviation_explained_rows += 1
                fully_explained_keys.add(dev_key)
                if differing_cols <= shadow_confirmed_cols:
                    deviation_shadow_confirmed_rows += 1

    # Unexpected rows first, then a small sample of fully-expected rows
    _all_sorted = sorted(rows_differ_set)
    _unexpected = [k for k in _all_sorted if k not in fully_explained_keys]
    _expected   = [k for k in _all_sorted if k in fully_explained_keys]
    differ_keys = _unexpected[:_MAX_DETAIL_ROWS] + _expected[:_DEVIATION_SAMPLE]

    for key in differ_keys:
        col_diffs: dict[str, tuple[Any, Any]] = {}
        for col in cols_to_check:
            c_col = f"{col}_computed" if f"{col}_computed" in inner.columns else col
            r_col = f"{col}_ref" if f"{col}_ref" in inner.columns else col
            if c_col not in inner.columns or r_col not in inner.columns:
                continue
            cv = _scalar(inner.loc[key, c_col]) if key in inner.index else None
            rv = _scalar(inner.loc[key, r_col]) if key in inner.index else None
            both_na = (cv is None) and (rv is None)
            if not both_na and cv != rv:
                col_diffs[col] = (cv, rv)

        # Full computed row (all columns, geometry excluded)
        comp_row: dict[str, Any] = {}
        if key in full_comp_idx.index:
            raw = full_comp_idx.loc[key]
            if isinstance(raw, pd.DataFrame):
                raw = raw.iloc[0]  # take first segment for multi-segment keys
            for k, v in raw.items():
                if hasattr(v, "geom_type"):  # shapely geometry
                    continue
                comp_row[k] = _scalar(v)

        # Full reference row
        ref_row: dict[str, Any] = {}
        if has_key and key in ref_idx.index:
            raw = ref_idx.loc[key]
            if isinstance(raw, pd.DataFrame):
                raw = raw.iloc[0]
            for k, v in raw.items():
                ref_row[k] = _scalar(v)

        row_diffs.append(RowDiff(
            key=key,
            col_diffs=col_diffs,
            computed_row=comp_row,
            reference_row=ref_row,
            expected_deviations=row_deviation_map.get(key, {}),
            shadow_confirmed_columns=row_shadow_confirmed_map.get(key, set()),
        ))

    return ValidationReport(
        stage=stage,
        city=city,
        n_computed=n_computed,
        n_reference=n_reference,
        rows_total=n_total,
        rows_differing=len(rows_differ_set),
        rows_only_computed=rows_only_computed,
        rows_only_reference=rows_only_reference,
        keys_only_computed=only_computed_keys[:20],
        keys_only_reference=only_reference_keys[:20],
        column_diffs=column_diffs,
        row_diffs=row_diffs,
        deviation_explained_rows=deviation_explained_rows,
        deviation_counts=deviation_counts,
        deviation_shadow_confirmed_rows=deviation_shadow_confirmed_rows,
        deviation_shadow_counts=deviation_shadow_counts,
        wall_time_seconds=time.monotonic() - t0,
    )


def _annotate_link_report(
    report: ValidationReport,
    way_attrs: pd.DataFrame,
    comp_links: pd.DataFrame,
    ref_links: pd.DataFrame,
    max_shown: int = 10,
) -> None:
    """Enrich a graph/links report with per-edge diagnostics for missing/extra edges.

    comp_links / ref_links must have columns source_osm_id, target_osm_id and cover
    the shared-vert population. All statistics are computed on the full population;
    report.keys_only_* (capped at 20) are only used for the displayed sample.

    For each mismatched-edge group, reports:
      - Structural breakdown: self-loops, reversed, wrong-target, source-absent
      - For wrong-target cases: what the source actually connects to in the other graph
      - FC-pair frequency table (full population)
      - One-way endpoint summary (sample)
    """
    from collections import Counter, defaultdict

    attrs = way_attrs.set_index("osm_id") if "osm_id" in way_attrs.columns else way_attrs

    def _is_one_way(val: object) -> bool:
        return isinstance(val, str) and val in ("ft", "tf")

    def _parse_keys(keys: list[str]) -> list[tuple[int, int]]:
        result = []
        for k in keys:
            parts = k.rsplit("_", 1)
            if len(parts) == 2:
                try:
                    result.append((int(parts[0]), int(parts[1])))
                except ValueError:
                    pass
        return result

    def _get_attr(osm_id: int, col: str, default: object = None) -> object:
        if osm_id not in attrs.index:
            return default
        row = attrs.loc[osm_id]
        val = row.get(col, default) if hasattr(row, "get") else getattr(row, col, default)
        return val

    def _describe_way(osm_id: int) -> str:
        fc = _get_attr(osm_id, "functional_class", "")
        ow = _get_attr(osm_id, "one_way", None)
        ow_tag = f" [{ow}]" if _is_one_way(ow) else ""
        return f"osm:{osm_id} {fc}{ow_tag}".strip()

    def _fc(osm_id: int) -> str:
        v = _get_attr(osm_id, "functional_class", "?")
        return str(v) if v else "?"

    # Full edge sets and per-source neighbour index.
    comp_set: set[tuple[int, int]] = set(
        zip(comp_links["source_osm_id"].astype(np.int64),
            comp_links["target_osm_id"].astype(np.int64))
    )
    ref_set: set[tuple[int, int]] = set(
        zip(ref_links["source_osm_id"].astype(np.int64),
            ref_links["target_osm_id"].astype(np.int64))
    )
    comp_only_pairs: set[tuple[int, int]] = comp_set - ref_set
    ref_only_pairs:  set[tuple[int, int]] = ref_set - comp_set

    # Out-neighbour sets: source -> {targets} in each full graph.
    comp_out: dict[int, set[int]] = defaultdict(set)
    ref_out:  dict[int, set[int]] = defaultdict(set)
    for s, t in comp_set:
        comp_out[s].add(t)
    for s, t in ref_set:
        ref_out[s].add(t)

    def _categorise(
        pairs: set[tuple[int, int]],
        other_full_set: set[tuple[int, int]],
        other_out: dict[int, set[int]],
    ) -> dict[str, list[tuple[int,int]]]:
        """Partition mismatched pairs into four structural categories."""
        cats: dict[str, list[tuple[int, int]]] = {
            "self_loop":     [],
            "reversed":      [],
            "wrong_target":  [],
            "source_absent": [],
        }
        for s, t in pairs:
            if s == t:
                cats["self_loop"].append((s, t))
            elif (t, s) in other_full_set:
                cats["reversed"].append((s, t))
            elif s in other_out:
                cats["wrong_target"].append((s, t))
            else:
                cats["source_absent"].append((s, t))
        return cats

    def _summarise(
        example_keys: list[str],
        full_pairs: set[tuple[int, int]],
        total: int,
        label: str,
        other_full_set: set[tuple[int, int]],
        other_out: dict[int, set[int]],
    ) -> None:
        if not full_pairs:
            return

        sample = _parse_keys(example_keys)  # up to 20 stored by compare_dataframes
        lines = [f"  {label} ({total} total, {min(len(sample), max_shown)} shown):"]

        # ── Structural breakdown (full population) ──────────────────────────
        cats = _categorise(full_pairs, other_full_set, other_out)
        breakdown_parts = []
        if cats["self_loop"]:
            breakdown_parts.append(f"self-loops: {len(cats['self_loop'])}")
        if cats["reversed"]:
            pct = round(100 * len(cats["reversed"]) / total)
            breakdown_parts.append(f"reversed direction: {len(cats['reversed'])} ({pct}%)")
        if cats["wrong_target"]:
            pct = round(100 * len(cats["wrong_target"]) / total)
            breakdown_parts.append(f"source connects elsewhere: {len(cats['wrong_target'])} ({pct}%)")
        if cats["source_absent"]:
            pct = round(100 * len(cats["source_absent"]) / total)
            breakdown_parts.append(f"source has no outgoing edges on other side: {len(cats['source_absent'])} ({pct}%)")
        if breakdown_parts:
            lines.append("    Breakdown: " + " | ".join(breakdown_parts))

        # ── Wrong-target examples: what does source connect to instead? ─────
        wt = cats["wrong_target"]
        if wt:
            # Show at most 3 sources with the most mismatched edges first.
            src_counts: Counter[int] = Counter(s for s, _ in wt)
            shown_srcs = 0
            for src, _ in src_counts.most_common(3):
                missing_tgts  = sorted(t for s, t in wt if s == src)
                present_tgts  = sorted(other_out[src])
                missing_descs = ", ".join(_describe_way(t) for t in missing_tgts[:3])
                present_descs = ", ".join(_describe_way(t) for t in present_tgts[:3])
                lines.append(
                    f"    {_describe_way(src)}:"
                    f"  missing→ {missing_descs}"
                    f"  |  other side→ {present_descs}"
                )
                shown_srcs += 1

        # ── Per-edge sample ─────────────────────────────────────────────────
        lines.append(f"  Sample ({min(len(sample), max_shown)} shown):")
        for src, tgt in sample[:max_shown]:
            cat = next(
                (k for k, lst in _categorise({(src, tgt)}, other_full_set, other_out).items() if lst),
                "?"
            )
            cat_tag = {"self_loop": "[loop]", "reversed": "[↔]",
                       "wrong_target": "[→?]", "source_absent": "[src∅]"}.get(cat, "")
            lines.append(f"    {_describe_way(src)}  →  {_describe_way(tgt)}  {cat_tag}")

        # ── FC-pair frequency table (full population) ──────────────────────
        fc_pairs = Counter((_fc(s), _fc(t)) for s, t in full_pairs)
        top = fc_pairs.most_common(5)
        if top and not (len(top) == 1 and top[0][0] == ("?", "?")):
            pair_strs = ", ".join(f"{s}→{t} ({n})" for (s, t), n in top)
            lines.append(f"    Top FC pairs: {pair_strs}")

        # ── One-way endpoint summary (sample) ──────────────────────────────
        one_way_count = sum(
            1 for s, t in sample[:max_shown]
            if _is_one_way(_get_attr(s, "one_way")) or _is_one_way(_get_attr(t, "one_way"))
        )
        if one_way_count:
            shown = min(len(sample), max_shown)
            pct = round(100 * one_way_count / shown)
            qualifier = f" of {shown} shown" if shown < total else ""
            lines.append(
                f"    ({one_way_count}/{shown}{qualifier} = {pct}% involve a one-way endpoint)"
            )

        report.report_notes.append("\n".join(lines))

    _summarise(
        report.keys_only_computed, comp_only_pairs,
        report.rows_only_computed, "Only in bna-core",
        ref_set, ref_out,
    )
    _summarise(
        report.keys_only_reference, ref_only_pairs,
        report.rows_only_reference, "Only in reference",
        comp_set, comp_out,
    )


def validate_graph(
    computed_verts: pd.DataFrame,
    computed_links: pd.DataFrame,
    ref_verts: pd.DataFrame,
    ref_links: pd.DataFrame,
    city: str,
    computed_road_to_osm: pd.DataFrame | None = None,
    ref_road_to_osm: pd.DataFrame | None = None,
    way_attrs: pd.DataFrame | None = None,
) -> tuple[ValidationReport, ValidationReport]:
    """Compare bna-core net_verts/net_links against brokenspoke-analyzer reference.

    bna-core and brokenspoke-analyzer use incompatible road_id spaces (OSM end-node
    IDs vs sequential SERIAL). Both sides are normalised to osm_id via the provided
    lookup DataFrames (road_id → osm_id) before comparison.

    Args:
        computed_verts: bna-core net_verts (vert_id, road_id).
        computed_links: bna-core net_links (source_vert, target_vert, link_cost, link_stress).
        ref_verts: reference net_verts (vert_id, road_id).
        ref_links: reference net_links (source_vert, target_vert, link_cost, link_stress).
        city: city slug for the report.
        computed_road_to_osm: DataFrame with (road_id, osm_id) for bna-core segments.
            Pass stressed_segments[["road_id", "osm_id"]].drop_duplicates().
        ref_road_to_osm: DataFrame with (road_id, osm_id) from reference ways_raw.parquet.

    Returns:
        (verts_report, links_report)

        verts_report: unique osm_id coverage — which OSM ways appear in bna-core but
            not the reference and vice versa.
        links_report: directed (source_osm_id, target_osm_id) edge-set comparison.
            Topology only — link_cost/stress are per-segment and don't survive the
            many-to-one collapse to osm_id pairs.
        way_attrs: Optional DataFrame with (osm_id, one_way, functional_class)
            used to annotate missing/extra links with per-edge diagnostics.
            Pass ref_ways_raw[["osm_id","one_way","functional_class"]].drop_duplicates("osm_id").
    """
    def _verts_to_osm(verts: pd.DataFrame, road_to_osm: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with unique osm_id values reachable from verts."""
        merged = verts[["road_id"]].merge(
            road_to_osm[["road_id", "osm_id"]].drop_duplicates(),
            on="road_id", how="left",
        )
        osm_ids = merged["osm_id"].dropna().drop_duplicates().astype(np.int64)
        return pd.DataFrame({"osm_id": osm_ids.values})

    def _links_to_osm(
        links: pd.DataFrame,
        verts: pd.DataFrame,
        road_to_osm: pd.DataFrame,
    ) -> pd.DataFrame:
        """Return DataFrame with unique (source_osm_id, target_osm_id) edge pairs."""
        rid_map: dict = dict(
            zip(verts["vert_id"].astype(np.int64), verts["road_id"].astype(np.int64))
        )
        osm_map: dict = dict(
            zip(
                road_to_osm["road_id"].astype(np.int64),
                road_to_osm["osm_id"].astype(np.int64),
            )
        )
        df = links.copy()
        df["src_road"] = df["source_vert"].astype(np.int64).map(rid_map)
        df["tgt_road"] = df["target_vert"].astype(np.int64).map(rid_map)
        df["source_osm_id"] = df["src_road"].map(osm_map)
        df["target_osm_id"] = df["tgt_road"].map(osm_map)
        df = df.dropna(subset=["source_osm_id", "target_osm_id"])
        df["source_osm_id"] = df["source_osm_id"].astype(np.int64)
        df["target_osm_id"] = df["target_osm_id"].astype(np.int64)
        df = df[["source_osm_id", "target_osm_id"]].drop_duplicates()
        df["_edge_key"] = (
            df["source_osm_id"].astype(str) + "_" + df["target_osm_id"].astype(str)
        )
        return df

    if computed_road_to_osm is None or ref_road_to_osm is None:
        raise ValueError(
            "validate_graph requires computed_road_to_osm and ref_road_to_osm — "
            "bna-core and brokenspoke-analyzer use incompatible road_id spaces. "
            "Pass stressed_segments[['road_id', 'osm_id']].drop_duplicates() "
            "and reference.load('ways_raw.parquet')[['road_id', 'osm_id']]."
        )

    comp_osm_verts = _verts_to_osm(computed_verts, computed_road_to_osm)
    ref_osm_verts = _verts_to_osm(ref_verts, ref_road_to_osm)

    comp_osm_links = _links_to_osm(computed_links, computed_verts, computed_road_to_osm)
    ref_osm_links = _links_to_osm(ref_links, ref_verts, ref_road_to_osm)

    # Restrict link comparison to edges where both endpoint osm_ids exist on
    # both sides. Links touching a vert that's only on one side are a downstream
    # consequence of the vert mismatch — counting them in the links report would
    # conflate two independent problems.
    shared_osm_ids = (
        set(comp_osm_verts["osm_id"].astype(np.int64))
        & set(ref_osm_verts["osm_id"].astype(np.int64))
    )
    def _filter_shared(df: pd.DataFrame) -> pd.DataFrame:
        mask = (
            df["source_osm_id"].astype(np.int64).isin(shared_osm_ids)
            & df["target_osm_id"].astype(np.int64).isin(shared_osm_ids)
        )
        return df[mask].copy()
    comp_osm_links_shared = _filter_shared(comp_osm_links)
    ref_osm_links_shared = _filter_shared(ref_osm_links)

    verts_report = compare_dataframes(
        comp_osm_verts,
        ref_osm_verts,
        stage="graph/verts",
        city=city,
        key_col="osm_id",
        columns=[],
    )
    n_links_excluded = (
        len(comp_osm_links) - len(comp_osm_links_shared)
        + len(ref_osm_links) - len(ref_osm_links_shared)
    )
    if n_links_excluded:
        verts_report.report_notes.append(
            f"{n_links_excluded} link-side rows excluded from links comparison "
            f"because their endpoint osm_id appears only on one side "
            f"(counted in verts report instead)."
        )

    links_report = compare_dataframes(
        comp_osm_links_shared,
        ref_osm_links_shared,
        stage="graph/links",
        city=city,
        key_col="_edge_key",
        columns=[],
    )

    # ── Annotate missing/extra links with per-edge diagnostics ─────────────
    if way_attrs is not None and (
        links_report.rows_only_computed or links_report.rows_only_reference
    ):
        _annotate_link_report(links_report, way_attrs, comp_osm_links_shared, ref_osm_links_shared)

    return verts_report, links_report


