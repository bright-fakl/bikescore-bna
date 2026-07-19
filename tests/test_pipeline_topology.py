"""Drift guard — ``PIPELINE`` is a valid topological order of the ``depends_on`` graph.

Core keeps a single static ordered ``PIPELINE`` and no runtime graph (index §2.1). The
app re-derives its DAG from the same ``depends_on`` metadata, so this stdlib-only check
is the one place that proves the hand-maintained ordering never lets a stage precede a
dependency. It also guards against duplicate stage names (which would corrupt the
``name -> output_dir`` tracking in ``score_city`` and the app engine alike).

Trivially green while ``PIPELINE`` is empty (Phase 38c); it becomes load-bearing as
38d/38e append stages.
"""

from __future__ import annotations

from bikescore.pipeline import PIPELINE


def test_stage_names_are_unique() -> None:
    names = [stage.name for stage in PIPELINE]
    assert len(names) == len(set(names)), f"duplicate stage name(s) in PIPELINE: {names}"


def test_pipeline_is_topologically_ordered() -> None:
    """Every stage's ``depends_on`` must appear strictly earlier in the list."""
    seen: set[str] = set()
    for stage in PIPELINE:
        for dep in stage.depends_on:
            assert dep in seen, (
                f"stage {stage.name!r} depends on {dep!r}, which does not precede it "
                "in PIPELINE (not a valid topological order)"
            )
        seen.add(stage.name)
