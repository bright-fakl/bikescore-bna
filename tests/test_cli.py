"""A7 — ``bikescore-bna`` CLI: pure-helper units, command wiring, and the score gate.

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

import bikescore_bna.cli as cli
from bikescore_bna.cli import _coerce, _parse_overrides, _scenario_arg, app
from bikescore_bna.deviations import KNOWN_DEVIATIONS
from bikescore_bna.validation import compare_dataframes

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
        city: object, out_dir: object, *, pbf_cache_dir: object = None, force: bool = False
    ) -> dict[str, Path]:
        captured["city"] = city.name  # type: ignore[attr-defined]
        captured["pbf_cache_dir"] = pbf_cache_dir
        captured["force"] = force
        return {"osm": Path("/x/osm-abc.pbf"), "boundary": Path("/x/boundary-def.geojson")}

    monkeypatch.setattr(cli, "acquire_city", _fake_acquire)
    result = runner.invoke(
        app,
        ["acquire", str(tmp_path), "--out-dir", str(tmp_path / "data"),
         "--pbf-cache-dir", str(tmp_path / "pbf")],
    )
    assert result.exit_code == 0
    assert captured["city"] == "Aspen"
    assert str(captured["pbf_cache_dir"]) == str(tmp_path / "pbf")
    assert "osm" in result.stdout and "boundary" in result.stdout


@pytest.mark.skipif(
    not (_WORKSPACE / "city.toml").exists() or not (_WORKSPACE / "datasets").is_dir(),
    reason="Aspen workspace (city.toml + datasets/) absent",
)
def test_score_reproduces_oracle(tmp_path: Path) -> None:
    out = tmp_path / "scores.parquet"
    result = runner.invoke(
        app,
        ["score", str(_WORKSPACE), "--scenario", "default",
         "--out-dir", str(tmp_path / "run"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    # stage outputs persist under the chosen --out-dir (not a discarded temp dir).
    assert (tmp_path / "run" / "scores" / "scores.parquet").exists()

    computed = pd.read_parquet(out)
    reference = pd.read_parquet(ORACLE / "scores" / "scores.parquet")
    cols = [c for c in reference.columns if c != "geoid20" and c in computed.columns]
    report = compare_dataframes(
        computed, reference, stage="scores", city="aspen-colorado",
        key_col="geoid20", columns=cols, deviations=KNOWN_DEVIATIONS,
    )
    assert report.passed and report.rows_differing == 0


# ── scenario show ─────────────────────────────────────────────────────────────


def test_scenario_show_prints_yaml() -> None:
    result = runner.invoke(app, ["scenario", "show", "default"])
    assert result.exit_code == 0
    assert "type:" in result.stdout  # raw bundled YAML on stdout


def test_scenario_show_out_writes_file(tmp_path: Path) -> None:
    dest = tmp_path / "copy.yaml"
    result = runner.invoke(app, ["scenario", "show", "default", "--out", str(dest)])
    assert result.exit_code == 0
    assert dest.is_file() and dest.read_text().startswith("type:")


def test_scenario_show_unknown_exits_2() -> None:
    result = runner.invoke(app, ["scenario", "show", "no-such-scenario-xyz"])
    assert result.exit_code == 2


# ── --set-file overrides ──────────────────────────────────────────────────────


def test_set_file_merges_and_inline_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    (tmp_path / "of.yaml").write_text("graph.low_stress_threshold: 3\nscoring.people: 42\n")
    seen: dict[str, object] = {}

    def _fake_score(inputs: object, config: object, **kw: object) -> object:
        seen["thr"] = config.graph.low_stress_threshold  # type: ignore[attr-defined]
        seen["people"] = config.scoring.people  # type: ignore[attr-defined]

        class _R:
            workdir = "/x"

            def output(self, *a: object, **k: object) -> Path:
                return Path("/nonexistent")

        return _R()

    monkeypatch.setattr(cli, "score_city", _fake_score)
    monkeypatch.setattr(cli, "_discover_inputs", lambda _d: {"osm": Path("/x")})
    result = runner.invoke(
        app,
        ["score", str(tmp_path), "--set-file", str(tmp_path / "of.yaml"),
         "--set", "scoring.people=99"],
    )
    assert result.exit_code == 0, result.stdout
    assert seen["thr"] == 3          # from the file
    assert seen["people"] == 99      # inline --set overrides the file


def test_export_from_reuses_without_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export --from <dir>` rebuilds a ScoreResult from disk and never runs the pipeline."""
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    run_dir = tmp_path / "run"
    (run_dir / "scores").mkdir(parents=True)  # a recognizable stage subdir

    def _must_not_score(*_a: object, **_k: object) -> object:
        raise AssertionError("score_city must not run when --from is given")

    seen: dict[str, object] = {}

    def _fake_bundle(result: object, *_a: object, **_k: object) -> list[Path]:
        seen["stage_dirs"] = set(result.stage_dirs)  # type: ignore[attr-defined]
        return [tmp_path / "out" / "bna.geojson"]

    monkeypatch.setattr(cli, "score_city", _must_not_score)
    monkeypatch.setattr(cli, "_discover_inputs", lambda _d: {"osm": Path("/x")})
    monkeypatch.setattr("bikescore_bna.export.export_bundle", _fake_bundle)

    result = runner.invoke(
        app, ["export", str(tmp_path), "--from", str(run_dir), "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.stdout
    assert seen["stage_dirs"] == {"scores"}


def test_export_from_empty_dir_exits_2(tmp_path: Path) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "city.osm.pbf").write_bytes(b"")
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["export", str(tmp_path), "--from", str(empty)])
    assert result.exit_code == 2


def test_set_file_missing_exits_2(tmp_path: Path) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "R"\ncountry = "united states"\n'
    )
    result = runner.invoke(
        app, ["score", str(tmp_path), "--set-file", str(tmp_path / "nope.yaml")]
    )
    assert result.exit_code == 2


def test_acquire_default_out_dir_is_city_datasets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    seen: dict[str, object] = {}

    def _fake_acquire(
        city: object, out_dir: object, *, pbf_cache_dir: object = None, force: bool = False
    ) -> dict[str, Path]:
        seen["out_dir"] = Path(out_dir)  # type: ignore[arg-type]
        return {"osm": Path("/x/osm.pbf")}

    monkeypatch.setattr(cli, "acquire_city", _fake_acquire)
    # no --out-dir → writes into <city>/datasets so `score <city>` finds it flag-free
    result = runner.invoke(app, ["acquire", str(tmp_path)])
    assert result.exit_code == 0
    assert seen["out_dir"] == tmp_path / "datasets"


# ── validate ──────────────────────────────────────────────────────────────────


def test_validate_missing_reference_exits_2(tmp_path: Path) -> None:
    (tmp_path / "city.toml").write_text(
        'name = "A"\nslug = "a"\nregion = "Colorado"\n'
        'country = "united states"\nfips_code = "0803620"\n'
    )
    result = runner.invoke(
        app, ["validate", str(tmp_path), "--reference", str(tmp_path / "no-such-ref")]
    )
    assert result.exit_code == 2


@pytest.mark.skipif(
    not (_WORKSPACE / "city.toml").exists() or not (_WORKSPACE / "datasets").is_dir(),
    reason="Aspen workspace (city.toml + datasets/) absent",
)
def test_validate_stress_matches_oracle() -> None:
    result = runner.invoke(
        app,
        ["validate", str(_WORKSPACE), "--reference", str(ORACLE), "--stage", "stress"],
    )
    assert result.exit_code == 0, result.stdout
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout
