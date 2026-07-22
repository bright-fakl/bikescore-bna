"""``bikescore-score`` — the minimal single-city CLI over the core public API.

Three commands, each a thin shell over the library (``build_config`` / ``acquire_city``
/ ``score_city``) — no workspace, no run store, no database:

    bikescore-score score    <city> [--scenario …] [--set k=v …] [--out …]
    bikescore-score acquire  <city> [--out-dir ./data]
    bikescore-score scenarios

``<city>`` is a path to a directory containing ``city.toml``. Raw inputs are read from
``<city>/datasets/`` (the ``acquire`` output layout) unless ``--datasets`` overrides.
This CLI is the app-free entry point; slug lookup, the multi-city project store, and
the web CLI all live in bikescore-app.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from bikescore import (
    CityIdentity,
    ScoreResult,
    acquire_city,
    build_config,
    discover_inputs,
    list_bundled_scenarios,
    score_city,
)
from bikescore.city import load_city
from bikescore.state_speeds import resolve_city_speed_defaults

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Single-city bicycle network analysis — score, acquire, and list scenarios.",
)
_console = Console()
_err = Console(stderr=True)

def _resolve_city_dir(city: str) -> Path:
    """Resolve *city* (a path to a directory containing ``city.toml``) to its directory."""
    p = Path(city)
    if (p / "city.toml").exists():
        return p
    _err.print(
        f"[red]City not found:[/red] {city!r} is not a directory containing city.toml. "
        f"Pass a path to a city directory (slug lookup lives in bikescore-app)."
    )
    raise typer.Exit(2)


def _load_identity(city_dir: Path) -> CityIdentity:
    try:
        return load_city(city_dir)
    except FileNotFoundError as exc:
        _err.print(f"[red]No city.toml in[/red] {city_dir}")
        raise typer.Exit(2) from exc


def _discover_inputs(datasets_dir: Path) -> dict[str, Path]:
    """CLI wrapper over ``discover_inputs`` with friendly errors (exit 2 if empty)."""
    if not datasets_dir.is_dir():
        _err.print(
            f"[red]No datasets directory:[/red] {datasets_dir}\n"
            f"Run [bold]bikescore-score acquire[/bold] first, or pass --datasets."
        )
        raise typer.Exit(2)
    inputs = discover_inputs(datasets_dir)
    if not inputs:
        _err.print(f"[red]No input files found in[/red] {datasets_dir}")
        raise typer.Exit(2)
    return inputs


def _coerce(value: str) -> Any:
    """Coerce a ``--set`` string value to int / float / bool / str."""
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    return value


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            _err.print(f"[red]--set expects key=value, got:[/red] {pair!r}")
            raise typer.Exit(2)
        key, _, raw = pair.partition("=")
        overrides[key.strip()] = _coerce(raw)
    return overrides


def _load_override_file(path: Path) -> dict[str, Any]:
    """Load dotted-key config overrides from a YAML mapping file (``key: value``).

    A reusable scenario carries structure; this file carries only the scalar one-offs —
    the same space as ``--set``. Inline ``--set`` flags take precedence over the file.
    """
    if not path.is_file():
        _err.print(f"[red]--set-file not found:[/red] {path}")
        raise typer.Exit(2)
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        _err.print("[red]--set-file must be a mapping of key: value.[/red]")
        raise typer.Exit(2)
    return {str(k): v for k, v in data.items()}


def _default_workdir(city_dir: Path) -> Path:
    """Persistent, timestamped stage-output root for a run: ``<city>/runs/<timestamp>``."""
    return city_dir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")


def _scenario_arg(scenario: str | None) -> str | Path | None:
    """A ``--scenario`` value is a bundled name, or a path to a YAML file."""
    if scenario is None:
        return None
    p = Path(scenario)
    if p.suffix in (".yaml", ".yml") or p.exists():
        return p
    return scenario


@app.command()
def score(
    city: Annotated[str, typer.Argument(help="Path to a city directory (containing city.toml).")],
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", "-s", help="Bundled scenario name or path to a YAML file."),
    ] = "default",
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Config override key=value (repeatable)."),
    ] = None,
    set_file: Annotated[
        Path | None,
        typer.Option("--set-file", help="YAML file of dotted-key overrides (merged under --set)."),
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option(
            "--out-dir",
            help="Persist all stage outputs here (default: <city>/runs/<timestamp>).",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Also copy scores.parquet to this file."),
    ] = None,
    datasets: Annotated[
        Path | None,
        typer.Option("--datasets", help="Raw-input directory (default: <city>/datasets)."),
    ] = None,
    to_stage: Annotated[
        str | None,
        typer.Option("--to", help="Stop after this stage (partial run)."),
    ] = None,
) -> None:
    """Score a city end-to-end and persist every stage's output for reuse.

    All stage outputs are written under ``--out-dir`` (default ``<city>/runs/<timestamp>``)
    and kept — point ``export --from <that dir>`` at them to export without recomputing.
    """
    city_dir = _resolve_city_dir(city)
    identity = _load_identity(city_dir)  # validate city.toml early
    datasets_dir = datasets if datasets is not None else city_dir / "datasets"
    inputs = _discover_inputs(datasets_dir)

    overrides = _parse_overrides(set_ or [])
    if set_file is not None:
        overrides = {**_load_override_file(set_file), **overrides}
    config = build_config(_scenario_arg(scenario), overrides)
    resolve_city_speed_defaults(config, identity)  # locale speed defaults from FIPS

    workdir = out_dir if out_dir is not None else _default_workdir(city_dir)
    _err.print(f"[dim]scoring {city_dir.name} ({len(inputs)} inputs, scenario={scenario})…[/dim]")
    result = score_city(inputs, config, workdir=workdir, to_stage=to_stage)

    produced = result.output("scores", "scores.parquet")
    if produced.exists():
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(produced, out)
            _console.print(f"[green]scores →[/green] {out}")
    else:
        _console.print(f"[yellow]scores not produced (stopped at {to_stage}).[/yellow]")
    _console.print(f"[green]stage outputs →[/green] {result.workdir}")


@app.command()
def acquire(
    city: Annotated[str, typer.Argument(help="Path to a city directory (containing city.toml).")],
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Directory to write inputs into (default: <city>/datasets)."),
    ] = None,
    pbf_cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--pbf-cache-dir",
            help="Shared regional-PBF cache dir (default: $BIKESCORE_PBF_CACHE or ~/.bikescore/pbf).",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-download the regional PBF even if cached."),
    ] = False,
) -> None:
    """Download the raw inputs (OSM, boundary, census, LODES) for a city."""
    city_dir = _resolve_city_dir(city)
    identity = _load_identity(city_dir)
    out_dir = out_dir if out_dir is not None else city_dir / "datasets"
    _err.print(f"[dim]acquiring inputs for {identity.name} → {out_dir}…[/dim]")
    files = acquire_city(identity, out_dir, pbf_cache_dir=pbf_cache_dir, force=force)

    table = Table("input", "path")
    for role in sorted(files):
        table.add_row(role, str(files[role]))
    _console.print(table)


@app.command()
def scenarios() -> None:
    """List the bundled scenario names available to ``--scenario``."""
    for name in list_bundled_scenarios():
        _console.print(name)


scenario_app = typer.Typer(no_args_is_help=True, help="Inspect bundled scenarios.")
app.add_typer(scenario_app, name="scenario")


@scenario_app.command("show")
def scenario_show(
    name: Annotated[str, typer.Argument(help="Bundled scenario name (e.g. 'default', 'default@1').")],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write the YAML to this file instead of stdout."),
    ] = None,
) -> None:
    """Dump a bundled scenario's YAML — copy it, edit, then run with ``--scenario FILE``."""
    from bikescore.scenarios import ScenarioNotFoundError, get_bundled_scenario

    try:
        text = get_bundled_scenario(name)
    except ScenarioNotFoundError as exc:
        _err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        _console.print(f"[green]scenario {name!r} →[/green] {out}")
    else:
        typer.echo(text, nl=False)  # raw YAML so it pipes / redirects cleanly


@app.command("export")
def export_cmd(
    city: Annotated[str, typer.Argument(help="Path to a city directory (containing city.toml).")],
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Single export target (see `export-list`)."),
    ] = None,
    bundle: Annotated[
        str | None,
        typer.Option("--bundle", "-b", help="Export a named bundle (default: bna if no --target)."),
    ] = None,
    file_format: Annotated[
        str | None,
        typer.Option("--format", "-f", help="geojson|shapefile|csv (required with --target)."),
    ] = None,
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Destination directory."),
    ] = Path("./export"),
    from_dir: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Reuse stage outputs from a prior `score`/run dir instead of recomputing.",
        ),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option(
            "--workdir",
            help="Where to persist stage outputs when computing (default: <city>/runs/<timestamp>).",
        ),
    ] = None,
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", "-s", help="Bundled scenario name or path to a YAML file."),
    ] = "default",
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Config override key=value (repeatable)."),
    ] = None,
    set_file: Annotated[
        Path | None,
        typer.Option("--set-file", help="YAML file of dotted-key overrides (merged under --set)."),
    ] = None,
    datasets: Annotated[
        Path | None,
        typer.Option("--datasets", help="Raw-input directory (default: <city>/datasets)."),
    ] = None,
) -> None:
    """Export a city's pipeline outputs to GeoJSON/Shapefile/CSV.

    Export a single target (``--target stress --format geojson``) or a whole bundle
    (``--bundle bna``, the default). Pass ``--from <run dir>`` to reuse a prior ``score``
    run's outputs without recomputing; otherwise the pipeline runs first, persisting stage
    outputs under ``--workdir`` (default ``<city>/runs/<timestamp>``).
    """
    from bikescore.export import export_bundle, export_target

    if target is not None and bundle is not None:
        _err.print("[red]Pass either --target or --bundle, not both.[/red]")
        raise typer.Exit(2)
    if target is not None and file_format is None:
        _err.print("[red]--format is required with --target.[/red]")
        raise typer.Exit(2)

    city_dir = _resolve_city_dir(city)
    identity = _load_identity(city_dir)
    datasets_dir = datasets if datasets is not None else city_dir / "datasets"
    inputs = _discover_inputs(datasets_dir)
    overrides = _parse_overrides(set_ or [])
    if set_file is not None:
        overrides = {**_load_override_file(set_file), **overrides}
    config = build_config(_scenario_arg(scenario), overrides)
    resolve_city_speed_defaults(config, identity)  # locale speed defaults from FIPS

    if from_dir is not None:
        try:
            result = ScoreResult.from_dir(from_dir)
        except FileNotFoundError as exc:
            _err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        _err.print(f"[dim]reusing stage outputs under {from_dir} (no recompute)…[/dim]")
    else:
        run_dir = workdir if workdir is not None else _default_workdir(city_dir)
        _err.print(f"[dim]scoring {city_dir.name} for export (scenario={scenario})…[/dim]")
        result = score_city(inputs, config, workdir=run_dir)

    try:
        if target is not None:
            written = export_target(
                result, identity, config, target, out, file_format=file_format, inputs=inputs,
            )
        else:
            written = export_bundle(
                result, identity, config, out, bundle=bundle or "bna", inputs=inputs,
            )
    except (ValueError, FileNotFoundError) as exc:
        _err.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(2) from exc

    for path in written:
        _console.print(f"[green]wrote[/green] {path}")
    _console.print(f"[green]{len(written)} file(s) →[/green] {out}")
    if from_dir is None:
        _console.print(f"[green]stage outputs →[/green] {result.workdir}")


@app.command("export-list")
def export_list_cmd() -> None:
    """List exportable targets (and the bundles that include them)."""
    from bikescore.export import (
        _EXPORT_TARGETS,
        DEFAULT_FORMATS,
        list_export_bundles,
        list_export_targets,
        target_bundles,
    )

    table = Table("target", "owner stage", "formats", "bundles")
    for name in list_export_targets():
        t = _EXPORT_TARGETS[name]
        table.add_row(
            name,
            t.owner_stage or "config",
            ", ".join(t.formats or DEFAULT_FORMATS[t.kind]),
            ", ".join(target_bundles(name)) or "—",
        )
    _console.print(table)
    _console.print(f"[dim]bundles: {', '.join(list_export_bundles())}[/dim]")


@app.command("validate")
def validate_cmd(
    city: Annotated[str, typer.Argument(help="Path to a city directory (containing city.toml).")],
    reference: Annotated[
        Path,
        typer.Option("--reference", "-r",
                     help="Reference dir with <stage>/<file>.parquet (e.g. tests/oracle/aspen)."),
    ],
    stage: Annotated[
        str | None,
        typer.Option("--stage", help="Validate only this stage (default: all stages)."),
    ] = None,
    datasets: Annotated[
        Path | None,
        typer.Option("--datasets", help="Raw-input directory (default: <city>/datasets)."),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Where to persist stage outputs (default: <city>/runs/<timestamp>)."),
    ] = None,
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", "-s", help="Bundled scenario name or path to a YAML file."),
    ] = "default",
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Config override key=value (repeatable)."),
    ] = None,
    set_file: Annotated[
        Path | None,
        typer.Option("--set-file", help="YAML file of dotted-key overrides (merged under --set)."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Do not annotate known SQL deviations as expected."),
    ] = False,
) -> None:
    """Score a city and compare each stage output against a reference directory.

    The reference holds ``<stage>/<file>.parquet`` (the tests/oracle/aspen layout, or a
    brokenspoke-analyzer export). Prints a per-stage pass/fail table and exits non-zero if
    any stage differs. Use --stage to check one stage (a faster partial run).
    """
    from bikescore.deviations import KNOWN_DEVIATIONS
    from bikescore.parity import validate_result

    city_dir = _resolve_city_dir(city)
    identity = _load_identity(city_dir)
    if not reference.is_dir():
        _err.print(f"[red]No reference directory:[/red] {reference}")
        raise typer.Exit(2)
    datasets_dir = datasets if datasets is not None else city_dir / "datasets"
    inputs = _discover_inputs(datasets_dir)

    overrides = _parse_overrides(set_ or [])
    if set_file is not None:
        overrides = {**_load_override_file(set_file), **overrides}
    config = build_config(_scenario_arg(scenario), overrides)
    resolve_city_speed_defaults(config, identity)  # locale speed defaults from FIPS

    run_dir = workdir if workdir is not None else _default_workdir(city_dir)
    _err.print(f"[dim]scoring {city_dir.name} for validation (scenario={scenario})…[/dim]")
    try:
        result = score_city(inputs, config, workdir=run_dir, to_stage=stage or None)
    except ValueError as exc:
        _err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    results = validate_result(
        result, reference, city=city_dir.name,
        stages=[stage] if stage else None,
        deviations=None if strict else KNOWN_DEVIATIONS,
    )
    if not results:
        _err.print(f"[yellow]No matching stage references found under[/yellow] {reference}")
        raise typer.Exit(2)

    table = Table("stage / case", "matched", "differing", "explained", "+comp", "+ref", "result")
    n_fail = 0
    n_skip = 0
    for sp in results:
        if sp.report is None:
            table.add_row(sp.case, "—", "—", "—", "—", "—", f"[dim]skip ({sp.skip_reason})[/dim]")
            n_skip += 1
            continue
        rep = sp.report
        verdict = "[green]PASS[/green]" if sp.passed else "[red]FAIL[/red]"
        if not sp.passed:
            n_fail += 1
        table.add_row(
            sp.case, str(rep.rows_total), str(rep.rows_differing),
            str(rep.deviation_explained_rows), str(rep.rows_only_computed),
            str(rep.rows_only_reference), verdict,
        )
    _console.print(table)

    compared = len(results) - n_skip
    if n_fail:
        _err.print(f"[red]{n_fail}/{compared} stage(s) differ from the reference.[/red]")
        raise typer.Exit(1)
    _console.print(
        f"[green]all {compared} compared stage(s) match[/green]"
        + (f" ([dim]{n_skip} skipped[/dim])" if n_skip else "")
    )


def main() -> None:
    """Console-script entry point (``bikescore-score``)."""
    app()


if __name__ == "__main__":
    main()
