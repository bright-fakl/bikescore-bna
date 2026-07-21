"""Typed field catalog + derived-field providers (design-review Appendix A.5).

Conditions reference fields from a per-decision **catalog** that classifies each
field as ``frame`` (a column already present) or ``derived`` (computed on demand
by a registered Python **provider**). The rules' *referenced-field set* drives
which providers run — this is what replaces the hardcoded intersection-context
join and realizes "rules declare their data dependencies".

A provider is a function ``(ProviderContext) -> dict[str, pd.Series]``; it declares
the field names it ``provides``. :func:`run_decision` collects the derived fields a
decision references, runs only the providers that supply them (once each), attaches
their columns, then evaluates the decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from bikescore.decision.model import Decision

# ── Field catalog ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldSpec:
    """Type + provenance for one referenceable field."""

    name: str
    kind: str  # "frame" | "derived"
    type: str = "str"  # enum | int | float | bool | str
    domain: tuple[Any, ...] | None = None
    provider: str | None = None  # required when kind == "derived"


@dataclass
class FieldCatalog:
    """A named set of :class:`FieldSpec` for one stage's decisions."""

    name: str
    fields: dict[str, FieldSpec] = field(default_factory=dict)

    def add(self, spec: FieldSpec) -> FieldCatalog:
        self.fields[spec.name] = spec
        return self

    def frame(self, name: str, type: str = "str", domain: tuple | None = None) -> FieldCatalog:
        return self.add(FieldSpec(name, "frame", type, domain))

    def derived(self, name: str, provider: str, type: str = "bool") -> FieldCatalog:
        return self.add(FieldSpec(name, "derived", type, provider=provider))

    def provider_for(self, name: str) -> str | None:
        spec = self.fields.get(name)
        return spec.provider if spec and spec.kind == "derived" else None


# ── Provider registry ─────────────────────────────────────────────────────────

@dataclass
class ProviderContext:
    """Inputs available to a provider.

    ``frame`` is the working DataFrame; ``needed`` is the decision's referenced-field
    set (so a provider can compute only the columns actually used); ``extras`` carries
    stage-supplied objects (e.g. ``nodes_df``, ``node_key``, ``adj_fc``, ``config``).
    """

    frame: pd.DataFrame
    needed: frozenset[str]
    extras: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)


ProviderFn = Callable[[ProviderContext], "dict[str, pd.Series]"]


@dataclass
class Provider:
    name: str
    provides: frozenset[str]
    fn: ProviderFn


_PROVIDERS: dict[str, Provider] = {}


def register_provider(name: str, provides: list[str]) -> Callable[[ProviderFn], ProviderFn]:
    """Decorator: register a derived-field provider under ``name``."""

    def deco(fn: ProviderFn) -> ProviderFn:
        _PROVIDERS[name] = Provider(name=name, provides=frozenset(provides), fn=fn)
        return fn

    return deco


def get_provider(name: str) -> Provider:
    if name not in _PROVIDERS:
        raise KeyError(f"unknown derived-field provider {name!r}")
    return _PROVIDERS[name]


# ── Orchestration ─────────────────────────────────────────────────────────────

def materialize_fields(
    frame: pd.DataFrame,
    needed: set[str],
    catalog: FieldCatalog,
    extras: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Attach every derived field in ``needed`` to ``frame`` by running providers.

    Each required provider runs once; it receives the full ``needed`` set so it can
    compute only the referenced columns.
    """
    extras = extras or {}
    provider_names: list[str] = []
    for f in needed:
        pname = catalog.provider_for(f)
        if pname and pname not in provider_names:
            provider_names.append(pname)
    if not provider_names:
        return frame
    frame = frame.copy()
    ctx = ProviderContext(frame=frame, needed=frozenset(needed), extras=extras)
    for pname in provider_names:
        produced = get_provider(pname).fn(ctx)
        for col, series in produced.items():
            frame[col] = series
        ctx = ProviderContext(frame=frame, needed=ctx.needed, extras=extras)
    return frame


def run_decision(
    decision: Decision,
    frame: pd.DataFrame,
    catalog: FieldCatalog,
    extras: Mapping[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Materialize the decision's referenced derived fields, then apply it."""
    needed = decision.referenced_fields()
    frame = materialize_fields(frame, needed, catalog, extras)
    return decision.apply(frame, variables=variables)


def run_pass(
    decision: Decision,
    pass_name: str,
    frame: pd.DataFrame,
    catalog: FieldCatalog,
    extras: Mapping[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Run a single named pass of ``decision`` (materializing only its fields).

    Used when a stage interleaves passes with Python steps (e.g. classify runs
    ``functional_class`` before bike_infra and ``class_promotion`` after it).
    """
    p = next((p for p in decision.passes if p.name == pass_name), None)
    if p is None:
        raise KeyError(f"decision {decision.name!r} has no pass {pass_name!r}")
    frame = materialize_fields(frame, p.table.referenced_fields(), catalog, extras)
    return p.table.apply(frame, variables=variables)
