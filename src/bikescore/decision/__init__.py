"""The decision DSL — one canonical, eval-free, analyzable decision engine.

Replaces the legacy ``RuleSet``/``Condition``/``Range``/``OsmMatcher`` with a single
model (ordered first-match rules of flat-AND clauses, ``set`` actions, ordered
passes), authoring views that compile to it, a typed field catalog, and Python
derived-field providers. See ``spec/design-review-config-rules-run-model.md``
Appendix A.
"""

from __future__ import annotations

from bikescore.decision.catalog import (
    FieldCatalog,
    FieldSpec,
    Provider,
    ProviderContext,
    get_provider,
    materialize_fields,
    register_provider,
    run_decision,
    run_pass,
)
from bikescore.decision.loader import (
    decision_from_doc,
    decision_to_terse,
    is_canonical_decision,
    load_decision,
    load_matcher,
    rule_to_terse,
)
from bikescore.decision.model import (
    CascadeRef,
    Clause,
    Decision,
    DecisionTable,
    FieldRef,
    LookupRef,
    MapRef,
    Matcher,
    MatchRow,
    Pass,
    Rule,
    VarRef,
    VarResolutionError,
)
from bikescore.decision.ops import OP_NAMES, apply_op, match_scalar

__all__ = [
    # model
    "CascadeRef",
    "Clause",
    "Rule",
    "DecisionTable",
    "Pass",
    "Decision",
    "FieldRef",
    "VarRef",
    "VarResolutionError",
    "LookupRef",
    "MapRef",
    "Matcher",
    "MatchRow",
    # ops
    "OP_NAMES",
    "apply_op",
    "match_scalar",
    # catalog / providers
    "FieldSpec",
    "FieldCatalog",
    "Provider",
    "ProviderContext",
    "register_provider",
    "get_provider",
    "materialize_fields",
    "run_decision",
    "run_pass",
    # authoring
    "load_decision",
    "load_matcher",
    "decision_to_terse",
    "decision_from_doc",
    "is_canonical_decision",
    "rule_to_terse",
]
