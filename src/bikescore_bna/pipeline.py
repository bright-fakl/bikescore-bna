"""The static ``PIPELINE`` and the ~20-line ``score_city`` driver.

Core keeps an explicit ordered ``PIPELINE`` list, not a runtime graph: the full
pipeline is a fixed 11-stage sequence (no config-driven stage inclusion — conditional
data is absorbed inside stages), so core needs only *ordering*. The dynamic DAG system
(topological sort, ancestors/descendants, reuse planner, ``--from/--to`` windows) lives
in the orchestration app, which re-derives its graph from the same ``depends_on``
metadata (index §2.1). One source of truth for deps; a stdlib drift-guard test asserts
``PIPELINE`` is a valid topological order of that graph.

``score_city`` is the database-free driver — no SQLite, no content-addressed hashing,
no run store, no ``graphlib``. It runs each stage into a caller-chosen ``workdir`` in
``PIPELINE`` order, tracking ``name -> output_dir``, so outputs are *persistent and
reusable* (not thrown away in a temp dir). ``ScoreResult.from_dir`` rebuilds a result
from such a folder, letting export/validate reuse a prior run without recomputing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bikescore_bna.stage import StageSpec, run_stage
from bikescore_bna.stages.attributes import ATTRIBUTES
from bikescore_bna.stages.census import CENSUS
from bikescore_bna.stages.connectivity import CONNECTIVITY
from bikescore_bna.stages.destinations import DESTINATIONS
from bikescore_bna.stages.graph import GRAPH
from bikescore_bna.stages.jobs import JOBS
from bikescore_bna.stages.neighborhood import NEIGHBORHOOD
from bikescore_bna.stages.parse import PARSE
from bikescore_bna.stages.scores import SCORES
from bikescore_bna.stages.segment import SEGMENT
from bikescore_bna.stages.stress import STRESS

if TYPE_CHECKING:
    from bikescore_bna.config import BNAConfig

# The fixed stage sequence. Phase 38d lands the walking skeleton parse -> stress; 38e
# appends graph -> neighborhood. Must stay a valid topological order of the
# ``depends_on`` graph (enforced by ``tests/test_pipeline_topology.py``).
PIPELINE: list[StageSpec] = [
    PARSE,
    CENSUS,
    JOBS,
    ATTRIBUTES,
    SEGMENT,
    STRESS,
    GRAPH,
    CONNECTIVITY,
    DESTINATIONS,
    SCORES,
    NEIGHBORHOOD,
]


@dataclass(frozen=True)
class ScoreResult:
    """What a ``score_city`` run produced.

    Attributes:
        stage_dirs: Maps each stage name that ran (or was ``pinned``) to the directory
            holding its output files. The final ``scores`` / ``neighborhood`` outputs
            are read from here by callers and the parity gate.
        workdir: The root under which every *computed* stage directory lives.
            ``pinned`` directories may lie outside it.
    """

    stage_dirs: dict[str, Path]
    workdir: Path

    def output(self, stage: str, filename: str) -> Path:
        """Path to ``filename`` inside ``stage``'s output directory."""
        return self.stage_dirs[stage] / filename

    @classmethod
    def from_dir(cls, workdir: Path) -> ScoreResult:
        """Rebuild a result from a folder ``score_city`` previously wrote to.

        Scans ``workdir`` for subdirectories named after pipeline stages and maps each
        present one to its directory. This is how export/validate reuse a prior run's
        outputs without recomputing the pipeline.

        Raises:
            FileNotFoundError: ``workdir`` holds no recognizable stage subdirectory.
        """
        workdir = Path(workdir)
        stage_dirs = {
            stage.name: workdir / stage.name
            for stage in PIPELINE
            if (workdir / stage.name).is_dir()
        }
        if not stage_dirs:
            raise FileNotFoundError(
                f"No pipeline stage output directories found under {workdir}. Expected "
                f"subdirectories named after stages (e.g. 'scores', 'stress')."
            )
        return cls(stage_dirs=stage_dirs, workdir=workdir)


def score_city(
    inputs: dict[str, Path],
    config: BNAConfig,
    *,
    workdir: Path | None = None,
    pinned: dict[str, Path] | None = None,
    to_stage: str | None = None,
) -> ScoreResult:
    """Score one city end-to-end with no database, workspace, or run store.

    Runs every stage in ``PIPELINE`` order into ``workdir``, resolving each stage's
    upstream directories from prior outputs (and ``pinned`` overrides) and its dataset
    inputs from ``inputs``. This is the engine-lite replacement for the app's
    ``PipelineEngine.execute_run`` minus SQLite recording, hashing, and JSON run logs.

    Args:
        inputs: Maps dataset-input name -> file path (e.g.
            ``{"osm": ..., "boundary": ..., "census": ..., "lodes_main": ...}``). Must
            cover every ``dataset_inputs`` name the stages that run declare.
        config: The effective ``BNAConfig`` (typically ``build_config(...)``).
        workdir: Directory to write stage outputs into (created if missing). Outputs
            persist here for reuse (e.g. by ``export`` via ``ScoreResult.from_dir``).
            Defaults to a fresh timestamped folder under ``./bikescore-bna-runs`` in the
            current directory — never a temp dir that gets silently discarded.
        pinned: Optional ``{stage_name: output_dir}`` of prebuilt stage outputs. A
            pinned stage is *not* recomputed; its directory is used verbatim as the
            upstream for later stages (e.g. supply a custom network for ``parse``).
        to_stage: Optional stage name to stop after (inclusive) — a partial run.

    Returns:
        A :class:`ScoreResult` mapping every stage that ran (or was pinned) to its
        output directory, plus the ``workdir`` root.

    Raises:
        ValueError: ``to_stage`` is not a stage in ``PIPELINE``.
        KeyError: A stage's declared upstream / dataset input was not available.
    """
    pinned = dict(pinned or {})
    stage_names = {stage.name for stage in PIPELINE}
    if to_stage is not None and to_stage not in stage_names:
        raise ValueError(
            f"to_stage={to_stage!r} is not a pipeline stage (known: {sorted(stage_names)})"
        )

    if workdir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        workdir = Path.cwd() / "bikescore-bna-runs" / stamp
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stage_dirs: dict[str, Path] = {}

    for stage in PIPELINE:
        if stage.name in pinned:
            stage_dirs[stage.name] = pinned[stage.name]
        else:
            output_dir = workdir / stage.name
            output_dir.mkdir(parents=True, exist_ok=True)
            upstream_dirs = {dep: stage_dirs[dep] for dep in stage.depends_on}
            dataset_paths = {name: inputs[name] for name in stage.dataset_inputs}
            run_stage(stage, upstream_dirs, dataset_paths, output_dir, config)
            stage_dirs[stage.name] = output_dir
        if to_stage is not None and stage.name == to_stage:
            break

    return ScoreResult(stage_dirs=stage_dirs, workdir=workdir)
