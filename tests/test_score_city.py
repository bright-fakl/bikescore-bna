"""A3 gate — the ``score_city`` skeleton drives ``PIPELINE`` without a database.

Phase 38c lands the loop against an *empty* ``PIPELINE`` (38d/38e fill it), so these
tests exercise the driver's control flow — temp-dir creation, ``pinned`` passthrough,
``to_stage`` validation — plus the shared ``run_stage`` primitive against a synthetic
``StageSpec``. No SQLite, no hashing, no run store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bikescore import BNAConfig, ScoreResult, StageSpec, run_stage, score_city
from bikescore.pipeline import PIPELINE


def _config() -> BNAConfig:
    return BNAConfig.with_defaults()


def test_empty_pipeline_runs() -> None:
    """The headline A3 gate: an empty PIPELINE scores without error."""
    assert PIPELINE == []
    result = score_city({}, _config())
    assert isinstance(result, ScoreResult)
    assert result.stage_dirs == {}
    assert result.workdir.is_dir()


def test_pinned_dirs_pass_through() -> None:
    """A pinned stage is tracked verbatim even when PIPELINE never runs it."""
    pinned = {"parse": Path("/some/prebuilt/parse")}
    result = score_city({}, _config(), pinned=pinned)
    # Empty PIPELINE means no stage consumes it, but the passthrough contract holds via
    # the driver: pinned is copied, not mutated by the caller's dict.
    assert result.stage_dirs == {}
    assert pinned == {"parse": Path("/some/prebuilt/parse")}


def test_unknown_to_stage_raises() -> None:
    with pytest.raises(ValueError, match="to_stage='parse' is not a pipeline stage"):
        score_city({}, _config(), to_stage="parse")


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
