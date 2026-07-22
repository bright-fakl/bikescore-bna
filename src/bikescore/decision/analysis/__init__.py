"""Decision-DSL analysis & validation substrate (design-review Appendix A.7).

One instrumentation over the canonical §A.2 engine — made tractable by the model's
stable rule ids, sweep provenance, and typed/enum catalog:

* :mod:`trace` — per-feature clause/rule/pass explanation.
* :mod:`coverage` — winning-rule tallies + never-fired detection.
* :mod:`contexts` — exact, finite **unique decision contexts** (threshold-
  partitioned), the cross-engine equivalence harness, and rule-version simulation.
* :mod:`checks` — static validation + shadowing / unreachable / exhaustiveness.
"""

from __future__ import annotations

from bikescore.decision.analysis.checks import (
    Issue,
    check_exhaustive,
    find_unreachable,
    validate_decision,
)
from bikescore.decision.analysis.contexts import (
    Cell,
    ContextRow,
    ContextTable,
    DiffRow,
    EquivalenceReport,
    build_partition,
    cross_engine_diff,
    decision_decider,
    field_cells,
    simulate,
    unique_contexts,
)
from bikescore.decision.analysis.coverage import (
    ColumnCoverage,
    CoverageReport,
    PassCoverage,
    authored_id,
    coverage,
)
from bikescore.decision.analysis.trace import (
    ClauseTrace,
    FeatureTrace,
    PassTrace,
    RuleTrace,
    trace,
)

__all__ = [
    # trace
    "trace",
    "FeatureTrace",
    "PassTrace",
    "RuleTrace",
    "ClauseTrace",
    # coverage
    "coverage",
    "CoverageReport",
    "PassCoverage",
    "ColumnCoverage",
    "authored_id",
    # contexts
    "unique_contexts",
    "ContextTable",
    "ContextRow",
    "Cell",
    "build_partition",
    "field_cells",
    "cross_engine_diff",
    "decision_decider",
    "simulate",
    "EquivalenceReport",
    "DiffRow",
    # checks
    "validate_decision",
    "find_unreachable",
    "check_exhaustive",
    "Issue",
]
