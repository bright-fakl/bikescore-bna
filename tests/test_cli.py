"""A7 — ``bikescore-score`` CLI: pure-helper units, command wiring, and the score gate.

The headline test (``test_score_reproduces_oracle``) is the A7 gate: the ``score``
command on the Aspen workspace reproduces the A5 ``scores`` table identically. It skips
when the frozen workspace (city.toml + datasets/) is absent. Everything else is CI-safe:
helper coercion, error paths (exit code 2), ``scenarios`` output, and the ``acquire``
command with a monkeypatched, network-free provider call.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import typer
from typer.testing import CliRunner

import bikescore.cli as cli
from bikescore.cli import _coerce, _parse_overrides, _scenario_arg, app
from bikescore.deviations import KNOWN_DEVIATIONS
from bikescore.validation import compare_dataframes

runner = CliRunner()

_WORKSPACE = Path("/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado")
ORACLE = Path(__file__).resolve().parent / "oracle" / "aspen"


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_coerce_types() -> None:
    assert _coerce("40") == 40
    assert isinstance(_coerce("40"), int)
    assert _coerce("3.5") == 3.5
    assert _coerce("true") is True
    assert _coerce("False") is False
    assert _coerce("track") == "track"


def test_parse_overrides_ok() -> None:
    assert _parse_overrides(["a.b=40", "c=true"]) == {"a.b": 40, "c": True}


def test_parse_overrides_rejects_bare() -> None:
    with pytest.raises(typer.Exit):
        _parse_overrides(["novalue"])


def test_scenario_arg_name_vs_path() -> None:
    assert _scenario_arg(None) is None
    assert _scenario_arg("default") == "default"
    assert _scenario_arg("custom.yaml") == Path("custom.yaml")


# ── Commands ─────────────────────────────────────────────────────────────────


def test_scenarios_lists_default() -> None:
    result = runner.invoke(app, ["scenarios"])
    assert result.exit_code == 0
    assert "default" in result.stdout


def test_unknown_city_exits_2() -> None:
    result = runner.invoke(app, ["score", "no-such-city-slug-xyz"])
    assert result.exit_code == 2


def test_score_missing_datasets_exits_2(tmp_path: Path) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "T"\nslug = "t"\nregion = "R"\ncountry = "united states"\n'
    )
    result = runner.invoke(app, ["score", str(tmp_path)])
    assert result.exit_code == 2


def test_acquire_command_prints_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "Aspen"\nslug = "aspen"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    captured: dict[str, object] = {}

    def _fake_acquire(
        city: object, out_dir: object, *, force: bool = False
    ) -> dict[str, Path]:
        captured["city"] = city.name  # type: ignore[attr-defined]
        captured["force"] = force
        return {"osm": Path("/x/osm-abc.pbf"), "boundary": Path("/x/boundary-def.geojson")}

    monkeypatch.setattr(cli, "acquire_city", _fake_acquire)
    result = runner.invoke(app, ["acquire", str(tmp_path), "--out-dir", str(tmp_path / "data")])
    assert result.exit_code == 0
    assert captured["city"] == "Aspen"
    assert "osm" in result.stdout and "boundary" in result.stdout


@pytest.mark.skipif(
    not (_WORKSPACE / "city.toml").exists() or not (_WORKSPACE / "datasets").is_dir(),
    reason="Aspen workspace (city.toml + datasets/) absent",
)
def test_score_reproduces_oracle(tmp_path: Path) -> None:
    out = tmp_path / "scores.parquet"
    result = runner.invoke(
        app, ["score", str(_WORKSPACE), "--scenario", "default", "--out", str(out)]
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()

    computed = pd.read_parquet(out)
    reference = pd.read_parquet(ORACLE / "scores" / "scores.parquet")
    cols = [c for c in reference.columns if c != "geoid20" and c in computed.columns]
    report = compare_dataframes(
        computed, reference, stage="scores", city="aspen-colorado",
        key_col="geoid20", columns=cols, deviations=KNOWN_DEVIATIONS,
    )
    assert report.passed and report.rows_differing == 0
