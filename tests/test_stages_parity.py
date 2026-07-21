"""A4 per-stage parity gate — the full 11-stage pipeline ``parse → … → neighborhood``
output must match the frozen bna-core Aspen oracle.

One ``score_city(inputs, build_config("default"), to_stage="neighborhood")`` run drives
every stage into a temp dir (no DB, no run store); each stage's output parquet is then
compared against its ``tests/oracle/aspen/<stage>/`` reference with the shared
``bikescore.parity`` harness (within ``KNOWN_DEVIATIONS``). ``stress.parquet``,
``scores.parquet`` and the three ``neighborhood`` tables are the headline gates.

The same harness backs the ``bikescore-score validate`` CLI, so this test and the CLI
compare identically.

The Aspen **input datasets** live in the frozen bna-core workspace, not this repo, so the
whole module ``skip``s when they are absent (cold clone / CI). Point at them with
``BIKESCORE_ASPEN_DATASETS`` or rely on the default workspace path. Oracle *outputs* are
regenerated via ``tests/oracle/regenerate_aspen.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bikescore import BNAConfig, ScoreResult, build_config, score_city
from bikescore.deviations import KNOWN_DEVIATIONS
from bikescore.parity import STAGE_FILES, _discover_dest_cases, compare_stage

ORACLE = Path(__file__).resolve().parent / "oracle" / "aspen"
_DEFAULT_DATASETS = Path(
    "/home/fabian/Projects/RideScore/BNA/bna-core-projects/aspen-colorado/datasets"
)


def _datasets_dir() -> Path | None:
    d = Path(os.environ.get("BIKESCORE_ASPEN_DATASETS", _DEFAULT_DATASETS))
    return d if d.is_dir() else None


def _aspen_inputs() -> dict[str, Path] | None:
    d = _datasets_dir()
    if d is None:
        return None
    from bikescore import discover_inputs

    inputs = discover_inputs(d)
    return inputs if len(inputs) == 5 else None


# case -> (rel_path, stage, key), including the per-type destination cluster tables
# discovered from the oracle.
_CASES: dict[str, tuple[str, str, object]] = {**STAGE_FILES, **_discover_dest_cases(ORACLE)}


pytestmark = pytest.mark.skipif(
    _aspen_inputs() is None,
    reason="Aspen input datasets absent — set BIKESCORE_ASPEN_DATASETS (see module docstring)",
)


@pytest.fixture(scope="module")
def score_result() -> ScoreResult:
    inputs = _aspen_inputs()
    assert inputs is not None  # guarded by pytestmark
    config: BNAConfig = build_config("default")
    return score_city(inputs, config, to_stage="neighborhood")


@pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
def test_stage_matches_oracle(case: str, score_result: ScoreResult) -> None:
    rel, stage, key = _CASES[case]
    parity = compare_stage(
        score_result, ORACLE, case, rel, stage, key,
        city="aspen-colorado", deviations=KNOWN_DEVIATIONS,
    )
    if parity.skip_reason is not None:
        pytest.skip(parity.skip_reason)
    assert parity.report is not None
    if not parity.report.passed:
        parity.report.print()
    assert parity.report.passed, (
        f"{rel}: {parity.report.rows_differing} differing rows, "
        f"{parity.report.rows_only_computed} only-computed, "
        f"{parity.report.rows_only_reference} only-reference"
    )
