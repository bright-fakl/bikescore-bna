"""Driver control-flow tests for the ``score_city`` skeleton + the ``run_stage`` primitive.

These exercise the driver's *control flow* — temp-dir creation, ordered execution,
``pinned`` passthrough, ``to_stage`` stop, and validation — against a **synthetic**
``PIPELINE`` (monkeypatched), so they stay independent of the real stage set that
Phase 38d populates. End-to-end parity against the Aspen oracle lives in
``tests/test_stages_parity.py``. No SQLite, no hashing, no run store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bikescore.pipeline as pipeline_mod
from bikescore import BNAConfig, ScoreResult, StageSpec, run_stage, score_city


def _config() -> BNAConfig:
    return BNAConfig.with_defaults()


def _touch_stage(name: str, depends_on: tuple[str, ...] = ()) -> StageSpec:
    """A trivial stage that just writes a marker file into its output dir."""

    def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
        (output_dir / f"{name}.txt").write_text("ok")

    return StageSpec(
        name=name,
        depends_on=depends_on,
        dataset_inputs=(),
        version="1.0.0",
        run=_run,
    )


def test_empty_pipeline_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty PIPELINE scores without error."""
    monkeypatch.setattr(pipeline_mod, "PIPELINE", [])
    result = score_city({}, _config())
    assert isinstance(result, ScoreResult)
    assert result.stage_dirs == {}
    assert result.workdir.is_dir()


def test_stages_run_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each stage runs into its own temp dir; downstream sees the upstream dir."""
    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE",
        [_touch_stage("a"), _touch_stage("b", depends_on=("a",))],
    )
    result = score_city({}, _config())
    assert set(result.stage_dirs) == {"a", "b"}
    assert (result.stage_dirs["a"] / "a.txt").read_text() == "ok"
    assert (result.stage_dirs["b"] / "b.txt").read_text() == "ok"


def test_pinned_dirs_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned stage is used verbatim and never recomputed."""
    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE",
        [_touch_stage("a"), _touch_stage("b", depends_on=("a",))],
    )
    pinned = {"a": Path("/some/prebuilt/a")}
    result = score_city({}, _config(), pinned=pinned)
    assert result.stage_dirs["a"] == Path("/some/prebuilt/a")
    # b consumed the pinned dir as its upstream and still ran.
    assert (result.stage_dirs["b"] / "b.txt").read_text() == "ok"
    assert pinned == {"a": Path("/some/prebuilt/a")}  # caller dict untouched


def test_to_stage_stops_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE",
        [_touch_stage("a"), _touch_stage("b", depends_on=("a",))],
    )
    result = score_city({}, _config(), to_stage="a")
    assert set(result.stage_dirs) == {"a"}


def test_unknown_to_stage_raises() -> None:
    with pytest.raises(ValueError, match="to_stage='nope' is not a pipeline stage"):
        score_city({}, _config(), to_stage="nope")


def test_run_stage_assembles_input_paths(tmp_path: Path) -> None:
    """The shared primitive maps deps -> dirs and datasets -> ``dataset:<name>`` keys."""
    captured: dict[str, Path] = {}

    def _run(input_paths: dict[str, Path], output_dir: Path, config: BNAConfig) -> None:
        captured.update(input_paths)
        (output_dir / "marker.txt").write_text("ok")

    spec = StageSpec(
        name="demo",
        depends_on=("upstream",),
        dataset_inputs=("osm",),
        version="1.0.0",
        run=_run,
    )
    up = tmp_path / "upstream"
    up.mkdir()
    osm = tmp_path / "city.osm.pbf"
    osm.write_bytes(b"")
    out = tmp_path / "out"
    out.mkdir()

    run_stage(spec, {"upstream": up}, {"osm": osm}, out, _config())

    assert captured == {"upstream": up, "dataset:osm": osm}
    assert (out / "marker.txt").read_text() == "ok"


def test_run_stage_missing_dependency_raises(tmp_path: Path) -> None:
    spec = StageSpec(
        name="demo",
        depends_on=("upstream",),
        dataset_inputs=(),
        version="1.0.0",
        run=lambda *_: None,
    )
    with pytest.raises(KeyError):
        run_stage(spec, {}, {}, tmp_path, _config())
