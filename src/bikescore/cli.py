"""``bikescore-score`` — the minimal single-city CLI over the core public API.

Three commands, each a thin shell over the library (``build_config`` / ``acquire_city``
/ ``score_city``) — no workspace, no run store, no database:

    bikescore-score score    <city> [--scenario …] [--set k=v …] [--out …]
    bikescore-score acquire  <city> [--out-dir ./data]
    bikescore-score scenarios

``<city>`` resolves to a directory containing ``city.toml``: either a path to that
directory, or a slug looked up under the global settings ``project_root``. Raw inputs
are read from ``<city>/datasets/`` (the ``acquire`` output layout) unless ``--datasets``
overrides. This CLI is the app-free entry point; the full multi-city / web CLI lives in
bikescore-app.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from bikescore import (
    CityIdentity,
    acquire_city,
    build_config,
    list_bundled_scenarios,
    score_city,
)
from bikescore.city import load_city

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Single-city bicycle network analysis — score, acquire, and list scenarios.",
)
_console = Console()
_err = Console(stderr=True)

# Raw-input discovery: role -> glob under the datasets dir (acquire's naming).
_INPUT_GLOBS = {
    "osm": "osm-*.pbf",
    "boundary": "boundary-*.geojson",
    "census": "census-*.parquet",
    "lodes_main": "lodes_main-*.csv",
    "lodes_aux": "lodes_aux-*.csv",
}


def _resolve_city_dir(city: str) -> Path:
    """Resolve *city* (a path to a city dir, or a settings slug) to its directory."""
    p = Path(city)
    if (p / "city.toml").exists():
        return p
    from bikescore.settings import get_city_dir

    try:
        return get_city_dir(city)
    except FileNotFoundError as exc:
        _err.print(
            f"[red]City not found:[/red] {city!r} is neither a directory with city.toml "
            f"nor a known slug under the settings project_root."
        )
        raise typer.Exit(2) from exc


def _load_identity(city_dir: Path) -> CityIdentity:
    try:
        return load_city(city_dir)
    except FileNotFoundError as exc:
        _err.print(f"[red]No city.toml in[/red] {city_dir}")
        raise typer.Exit(2) from exc


def _discover_inputs(datasets_dir: Path) -> dict[str, Path]:
    """Find the raw inputs under *datasets_dir*; error if none present."""
    if not datasets_dir.is_dir():
        _err.print(
            f"[red]No datasets directory:[/red] {datasets_dir}\n"
            f"Run [bold]bikescore-score acquire[/bold] first, or pass --datasets."
        )
        raise typer.Exit(2)
    inputs: dict[str, Path] = {}
    for role, pattern in _INPUT_GLOBS.items():
        hits = sorted(datasets_dir.glob(pattern))
        if hits:
            inputs[role] = hits[0]
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
    city: Annotated[str, typer.Argument(help="City directory (with city.toml) or slug.")],
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", "-s", help="Bundled scenario name or path to a YAML file."),
    ] = "default",
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Config override key=value (repeatable)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write scores.parquet here (default: ./scores.parquet)."),
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
    """Score a city end-to-end and write the block-level ``scores`` table."""
    city_dir = _resolve_city_dir(city)
    _load_identity(city_dir)  # validate city.toml early
    datasets_dir = datasets if datasets is not None else city_dir / "datasets"
    inputs = _discover_inputs(datasets_dir)

    config = build_config(_scenario_arg(scenario), _parse_overrides(set_ or []))

    _err.print(f"[dim]scoring {city_dir.name} ({len(inputs)} inputs, scenario={scenario})…[/dim]")
    result = score_city(inputs, config, to_stage=to_stage)

    out_path = out if out is not None else Path("scores.parquet")
    produced = result.output("scores", "scores.parquet")
    if produced.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, out_path)
        _console.print(f"[green]scores →[/green] {out_path}")
    else:
        _console.print(f"[yellow]scores not produced (stopped at {to_stage}).[/yellow]")
    _err.print(f"[dim]all stage outputs under {result.workdir}[/dim]")


@app.command()
def acquire(
    city: Annotated[str, typer.Argument(help="City directory (with city.toml) or slug.")],
    out_dir: Annotated[
        Path, typer.Option("--out-dir", help="Directory to write inputs into."),
    ] = Path("./data"),
    force: Annotated[
        bool, typer.Option("--force", help="Re-download the regional PBF even if cached."),
    ] = False,
) -> None:
    """Download the raw inputs (OSM, boundary, census, LODES) for a city."""
    city_dir = _resolve_city_dir(city)
    identity = _load_identity(city_dir)
    _err.print(f"[dim]acquiring inputs for {identity.name}…[/dim]")
    files = acquire_city(identity, out_dir, force=force)

    table = Table("input", "path")
    for role in sorted(files):
        table.add_row(role, str(files[role]))
    _console.print(table)


@app.command()
def scenarios() -> None:
    """List the bundled scenario names available to ``--scenario``."""
    for name in list_bundled_scenarios():
        _console.print(name)


def main() -> None:
    """Console-script entry point (``bikescore-score``)."""
    app()


if __name__ == "__main__":
    main()
