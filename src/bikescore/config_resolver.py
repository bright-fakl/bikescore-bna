"""Effective-config resolver — single self-contained scenario (Phase 35j / supplement M1–M4).

A city points at **exactly one** scenario; there is no scenario *stack*. The effective
``BNAConfig`` for a run is:

    scenario (self-contained structural source)
      ⊕ city overrides  (scalar-only locale facts)
      ⊕ --set           (per-run scalar one-offs)
      ⊕ destination filter (city include/exclude; can only shrink the active set)

The scenario carries its attributes + rulesets + destinations + default scalar config
**together** (M4), so producers (attributes) and consumers (rules) can never desync.

Two scenario *kinds* load differently:

* ``type: complete`` — read as the **self-contained** source. Structural namespaces
  (attributes, rulesets, destinations, intersection_attributes) are **block-replaced**
  honouring deletion: absence in the scenario = removed (no implicit re-inherit from
  package defaults). This makes the stored scenario the true source of truth.
* sparse / untyped — read as **deltas over package defaults** (additive keyed-merge),
  for scalar-only or minimal scenarios that intentionally inherit the rest.

A run-level ``--scenario`` *replaces* the city's scenario for that run (no stacking).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bikescore.config import BNAConfig


class ConfigResolverError(Exception):
    """Raised when a config layer references an unknown namespace or entry."""


# Config namespaces that map directly to a BNAConfig sub-config attribute.
_STAGE_NAMESPACES: frozenset[str] = frozenset({
    "city", "imputation", "stress", "graph",
    "connectivity", "scoring", "export", "cache",
})

# Top-level scalar BNAConfig fields, addressed under the ``globals`` namespace.
_GLOBAL_FIELDS: frozenset[str] = frozenset({
    "max_trip_distance", "block_road_buffer", "block_road_min_length",
    "output_srid", "min_path_length", "min_bbox_length",
})

# Ruleset-typed entries (value is a decision document → Decision), per namespace.
_RULESET_ENTRIES: dict[str, frozenset[str]] = {
    "stress": frozenset({"segment_rules", "intersection_rules"}),
}


# ── Public API ────────────────────────────────────────────────────────────────


# Structural namespaces — the producer/consumer-coupled content carried *together*
# by a self-contained scenario (supplement M4). A complete scenario block-replaces
# these honouring deletion (absence = removed); a sparse scenario merges deltas.
_RULESET_FIELDS: tuple[tuple[str, str], ...] = (
    ("stress", "segment_rules"),
    ("stress", "intersection_rules"),
)


def _clear_structural(config: BNAConfig) -> None:
    """Empty the structural namespaces so a complete scenario block-replaces them.

    Honouring deletion (supplement M1/M5): start from *empty* containers, never the
    package defaults, so a name the scenario omits is genuinely gone — not silently
    re-inherited. ``_apply_layer`` then re-populates from the scenario's own lists,
    reusing the proven keyed-merge onto an empty base.
    """
    from bikescore.attributes import AttributeRegistry
    from bikescore.destinations import DestinationRegistry

    config.attributes = AttributeRegistry()
    config.destinations = DestinationRegistry.from_dict({"destinations": []})
    config.intersection_attributes = []
    for ns, field_name in _RULESET_FIELDS:
        setattr(getattr(config, ns), field_name, None)


def _apply_rule_sets(config: BNAConfig, rule_sets_section: dict, ref: str) -> None:
    """Apply a complete scenario's ``rule_sets:`` block (each value → Decision)."""
    for ruleset_name, ruleset_val in rule_sets_section.items():
        ns, field_name = _RULESET_LOCATIONS.get(ruleset_name, (None, None))
        if ns is None:
            raise ConfigResolverError(
                f"Unknown ruleset {ruleset_name!r} in complete scenario {ref!r}."
            )
        _set_entry(ns, getattr(config, ns), field_name, ruleset_val)


def _variable_declaration(val: Any) -> tuple[Any, bool | None]:
    """Normalize a ``variables:`` entry into ``(value, required)`` (Phase 36b).

    The mapping form ``{default: <v>, required: <bool>}`` declares a value plus a
    requiredness flag; a bare scalar (or any dict not using those keys) is the value
    with no requiredness change (``required`` is ``None``)."""
    if (
        isinstance(val, dict)
        and ("default" in val or "required" in val)
        and set(val) <= {"default", "required"}
    ):
        return val.get("default"), val.get("required")
    return val, None


def _emit_variable(value: Any, required: bool) -> Any:
    """Serialize one variable to disk form: mapping ``{default, required}`` when
    required, bare value otherwise."""
    if not required:
        return value
    decl: dict = {}
    if value is not None:
        decl["default"] = value
    decl["required"] = True
    return decl


def serialize_complete_config(config: BNAConfig) -> dict:
    """Serialize all BNAConfig fields to a complete-scenario dict.

    Produces the ``config:`` and ``rule_sets:`` sections for a type:complete scenario.
    Every key is explicit — no inheritance from any built-in defaults.
    """
    import dataclasses as _dc
    from pathlib import Path as _Path

    from bikescore.decision import Decision

    def _safe_val(v: Any) -> Any:
        """Convert values to YAML-safe types (no !!python tags)."""
        if isinstance(v, _Path):
            return str(v)
        if _dc.is_dataclass(v) and not isinstance(v, type):
            return {f.name: _safe_val(getattr(v, f.name)) for f in _dc.fields(v)}
        if isinstance(v, (list, tuple)):
            return [_safe_val(x) for x in v]
        return v

    out_config: dict = {}

    # Global scalars
    globals_section: dict = {}
    for fld in sorted(_GLOBAL_FIELDS):
        val = getattr(config, fld)
        globals_section[fld] = _safe_val(val)
    if globals_section:
        out_config["globals"] = globals_section

    # Per-stage sub-configs (scalars only; rule sets go to rule_sets section)
    ruleset_fields = {
        ns: entries for ns, entries in _RULESET_ENTRIES.items()
    }
    for ns in sorted(_STAGE_NAMESPACES):
        sub = getattr(config, ns)
        section: dict = {}
        for fld in _dc.fields(sub):
            name = fld.name
            val = getattr(sub, name)
            if name in ruleset_fields.get(ns, frozenset()):
                continue  # rulesets go to rule_sets section
            section[name] = _safe_val(val)
        if section:
            out_config[ns] = section

    # Destinations (atomic block or catalog name)
    if config.destinations is not None:
        if config.destinations._source_name is not None:
            out_config["destinations"] = config.destinations._source_name
        else:
            out_config["destinations"] = list(config.destinations.to_dict().values())

    # Rule sets (use default loader for fields that are None)
    out_rule_sets: dict = {}
    for ruleset_name, (ns, field_name) in sorted(_RULESET_LOCATIONS.items()):
        sub = getattr(config, ns)
        decision = getattr(sub, field_name)
        if decision is None:
            if ruleset_name in _RULESET_DEFAULT_LOADERS:
                decision = _load_default_ruleset(ruleset_name)
            else:
                continue
        if isinstance(decision, Decision):
            # An unmodified built-in ruleset is serialized from its *authored* terse doc
            # (compact ``for:`` sweeps / nested scope-gated rules) rather than a
            # ``decision_to_terse`` re-render: the importer cannot reconstruct nested
            # authoring (Phase 36c), and the verbatim authored form is both smaller and
            # the editable source of truth (Phase 36a). Modified/replacement decisions
            # have no authored doc at this layer, so fall back to the importer.
            from bikescore.decision import decision_to_terse
            if (
                ruleset_name in _RULESET_AUTHORED_LOADERS
                and decision.to_dict() == _load_default_ruleset(ruleset_name).to_dict()
            ):
                out_rule_sets[ruleset_name] = _load_default_ruleset_doc(ruleset_name)
            else:
                out_rule_sets[ruleset_name] = decision_to_terse(decision)
        else:
            out_rule_sets[ruleset_name] = decision

    # Intersection attributes (full list, terse match form)
    if config.intersection_attributes is not None:
        out_config["intersection_attributes"] = [
            a.to_dict() for a in config.intersection_attributes
        ]

    # Attributes (serializable DSL + primary attributes; Python CustomAttributes
    # omitted — their callables cannot round-trip). Insertion order is preserved so
    # producers (e.g. the primary ``width``) precede consumers (``width_parsed``).
    if config.attributes is not None:
        from bikescore.attributes import DecisionAttribute, PrimaryAttribute
        dsl_attributes = [
            f.to_dict() for f in config.attributes._attributes.values()
            if isinstance(f, (DecisionAttribute, PrimaryAttribute))
        ]
        if dsl_attributes:
            out_config["attributes"] = dsl_attributes

    # User-defined variables (Phase 36b: emit the {default, required} mapping form for
    # variables declared required; bare scalar otherwise).
    if config.variables:
        out_config["variables"] = {
            k: _emit_variable(v, k in config.required_variables)
            for k, v in config.variables.items()
        }

    return {"config": out_config, "rule_sets": out_rule_sets}


def _merge_destinations(registry: Any, entries: list) -> None:
    """Keyed-merge a list of destination entries into an existing registry.

    Accepts both the *compact* catalog form (matchers nested under ``match:``) and the
    *full* ``to_dict()`` form emitted by ``serialize_complete_config`` (explicit
    ``node_match``/``area_match``/... keys). The full form is what a complete foundation
    such as bundled ``default`` carries, so a ``base: default`` layer round-trips losslessly.
    """
    from bikescore.destinations import (
        _catalog_type_from_compact,
        _destination_type_from_dict,
    )

    def _is_full_form(d: dict) -> bool:
        # The ``to_dict()`` form uses explicit matcher keys; the compact catalog form
        # nests matchers under ``match:``. Either discriminator is unambiguous.
        return any(
            k in d for k in ("node_match", "area_match", "exclude_match", "way_match")
        )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        enabled = entry.get("enabled")
        if enabled is False:
            if name in registry._types:
                registry.disable(name)
        elif set(entry.keys()) <= {"name", "enabled"}:
            if name in registry._types:
                registry.enable(name)
        else:
            new_type = (
                _destination_type_from_dict(entry)
                if _is_full_form(entry)
                else _catalog_type_from_compact(entry)
            )
            if name in registry._types:
                registry.replace(name, new_type)
            else:
                registry.register(new_type)


def _merge_intersection_attributes(attrs: list, entries: list) -> list:
    """Keyed-merge a list of compact intersection-attribute entries by ``name``.

    Mirrors :func:`_merge_destinations`: ``{name, enabled: false}`` deactivates an
    attribute, ``{name}`` (re)enables it, and ``{name, match}`` adds or replaces it.
    Order is preserved; new attributes append.
    """
    from bikescore.intersection_attributes import IntersectionAttribute

    by_name = {a.name: a for a in attrs}
    order = [a.name for a in attrs]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        enabled = entry.get("enabled")
        if enabled is False:
            if name in by_name:
                by_name[name].enabled = False
            continue
        if set(entry.keys()) <= {"name", "enabled"}:
            if name in by_name:
                by_name[name].enabled = True
            continue
        new_attr = IntersectionAttribute.from_dict(entry)
        if name not in by_name:
            order.append(name)
        by_name[name] = new_attr
    return [by_name[n] for n in order]


# ── Layer application ─────────────────────────────────────────────────────────


def _apply_layer(
    config: BNAConfig, layer: dict | None, provenance: dict[str, str], label: str,
) -> None:
    for namespace, entries in (layer or {}).items():
        if namespace == "description":
            continue
        if namespace == "destinations":
            from bikescore.destinations import DestinationRegistry

            if isinstance(entries, str):
                raise ConfigResolverError(
                    f"Destination catalog reference by name ({entries!r}) is not "
                    "resolvable in the scoring core — inline the destination list in "
                    "the scenario. Catalog libraries are an orchestration-layer feature."
                )
            elif isinstance(entries, dict) and entries.get("replace") is True:
                types_raw = entries.get("types", [])
                config.destinations = DestinationRegistry.from_dict({"destinations": types_raw})
            elif isinstance(entries, list):
                if config.destinations is None:
                    config.destinations = DestinationRegistry.from_dict({"destinations": entries})
                else:
                    _merge_destinations(config.destinations, entries)
            else:
                config.destinations = DestinationRegistry.from_dict({"destinations": entries})
            provenance["destinations"] = label
            continue
        if namespace == "intersection_attributes":
            if not isinstance(entries, list):
                raise ConfigResolverError(
                    "'intersection_attributes' must be a list of {name, match} entries."
                )
            from bikescore.intersection_attributes import default_intersection_attributes
            if config.intersection_attributes is None:
                config.intersection_attributes = default_intersection_attributes()
            config.intersection_attributes = _merge_intersection_attributes(
                config.intersection_attributes, entries
            )
            provenance["intersection_attributes"] = label
            continue
        if namespace == "attributes":
            if not isinstance(entries, list):
                raise ConfigResolverError("'attributes' must be a list of attribute definitions.")
            from bikescore.attributes import AttributeRegistry, attribute_from_dict
            if config.attributes is None:
                config.attributes = AttributeRegistry()
            for raw_attribute in entries:
                feat = attribute_from_dict(raw_attribute)
                if feat.name in config.attributes._attributes:
                    config.attributes._attributes[feat.name] = feat
                else:
                    config.attributes.register(feat)
            provenance["attributes"] = label
            continue
        if namespace == "globals":
            for key, val in (entries or {}).items():
                _set_global(config, key, val)
                provenance[f"globals.{key}"] = label
            continue
        if namespace == "variables":
            if not isinstance(entries, dict):
                raise ConfigResolverError("'variables' must be a mapping of name: value.")
            for key, val in entries.items():
                value, required = _variable_declaration(val)
                config.variables[key] = value
                if required is True:
                    config.required_variables.add(key)
                elif required is False:
                    config.required_variables.discard(key)
                provenance[f"variables.{key}"] = label
            continue
        if namespace in _STAGE_NAMESPACES:
            sub = getattr(config, namespace)
            for key, val in (entries or {}).items():
                _set_entry(namespace, sub, key, val)
                provenance[f"{namespace}.{key}"] = label
            continue
        raise ConfigResolverError(
            f"Unknown config namespace '{namespace}' in layer '{label}'."
        )


def _apply_set(
    config: BNAConfig, run_set: dict[str, Any], provenance: dict[str, str]
) -> None:
    for raw_key, raw_val in run_set.items():
        val = _coerce(raw_val)
        if "." in raw_key:
            namespace, key = raw_key.split(".", 1)
        else:
            namespace, key = "globals", raw_key
        if namespace == "globals":
            _set_global(config, key, val)
        elif namespace == "variables":
            config.variables[key] = val
        elif namespace in _STAGE_NAMESPACES:
            _set_entry(namespace, getattr(config, namespace), key, val)
        else:
            raise ConfigResolverError(
                f"--set '{raw_key}': unknown config namespace '{namespace}'."
            )
        provenance[f"{namespace}.{key}"] = "--set"


def _to_decision(val: Any) -> Any:
    """Coerce a scenario/--set ruleset value into a Decision.

    Accepts the canonical ``{name, passes}`` form (as written by
    ``sparse_config_diff``) or the terse authored form (``{rules: [...]}`` /
    a list of authored passes).
    """
    from bikescore.decision import Decision, decision_from_doc

    if isinstance(val, Decision):
        return val
    return decision_from_doc(val)


def _set_entry(namespace: str, sub: Any, key: str, val: Any) -> None:
    if key in _RULESET_ENTRIES.get(namespace, frozenset()):
        val = _to_decision(val)
    if not hasattr(sub, key):
        raise ConfigResolverError(f"Unknown config entry '{namespace}.{key}'.")
    if isinstance(val, dict):
        existing = getattr(sub, key)
        if isinstance(existing, dict):
            # Plain dict entries (e.g. scoring.category_weights,
            # stress.crossing_speed_defaults) merge last-wins per key, so a sparse
            # scenario/city override need only declare the keys that differ.
            val = {**existing, **val}
        elif dataclasses.is_dataclass(existing) and not isinstance(existing, type):
            val = type(existing)(**val)
    setattr(sub, key, val)


def _set_global(config: BNAConfig, key: str, val: Any) -> None:
    if key not in _GLOBAL_FIELDS:
        raise ConfigResolverError(f"Unknown global config field '{key}'.")
    setattr(config, key, val)


def _coerce(value: Any) -> Any:
    """Coerce a ``--set`` string into bool/int/float, leaving non-strings untouched."""
    if not isinstance(value, str):
        return value
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


# Named rulesets → (config namespace, sub-config field). Used by the CLI/web to read
# the effective ruleset (scenario-driven) and tell whether it differs from defaults.
_RULESET_LOCATIONS: dict[str, tuple[str, str]] = {
    "stress_segment": ("stress", "segment_rules"),
    "stress_intersection": ("stress", "intersection_rules"),
}

# Canonical default loaders, used when a config field is lazily None (e.g.
# classification.functional_class_rules has no eager default).
_RULESET_DEFAULT_LOADERS: dict[str, tuple[str, str]] = {
    "stress_segment": ("bikescore.rules.stress_segment", "default_segment_stress_rules"),
    "stress_intersection": ("bikescore.rules.stress_intersection", "default_intersection_stress_rules"),
}


def _load_default_ruleset(ruleset_name: str):
    import importlib

    module_name, loader_name = _RULESET_DEFAULT_LOADERS[ruleset_name]
    return getattr(importlib.import_module(module_name), loader_name)()


# Authored-doc loaders (Phase 36a): return the *terse source document* for a built-in
# ruleset — the parsed ``rules/data/*.yaml`` (real set names, ``for:`` sweeps), not a
# canonical re-render. The rule-builder seeds + clones from this so editing a built-in
# starts from the authored source, never a flattened ``decision_to_terse`` snapshot.
_RULESET_AUTHORED_LOADERS: dict[str, tuple[str, str]] = {
    "stress_segment": ("bikescore.rules.stress_segment", "authored_segment_stress_doc"),
    "stress_intersection": (
        "bikescore.rules.stress_intersection", "authored_intersection_stress_doc"),
}


def _load_default_ruleset_doc(ruleset_name: str) -> dict:
    import importlib

    module_name, loader_name = _RULESET_AUTHORED_LOADERS[ruleset_name]
    return getattr(importlib.import_module(module_name), loader_name)()




# ── Core public API: build a BNAConfig from a scenario (Phase 38b) ─────────────
# This replaces bna-core's workspace-coupled ``resolve_config``/``_load_scenario``:
# no city dir, no ``city_config`` layering, no provenance. The app (bikescore-app)
# re-adds workspace resolution on top of ``_config_from_meta``.


def _config_from_meta(meta: dict, label: str) -> BNAConfig:
    """Deserialize one scenario document (``meta``) into a ``BNAConfig``.

    ``type: complete`` → self-contained source: structural namespaces are cleared and
    block-replaced from the scenario's own ``config:`` + ``rule_sets:`` (deletion-honest).
    sparse/untyped → deltas additively merged over ``BNAConfig.with_defaults()``.
    """
    from bikescore.config import BNAConfig

    scenario_type = meta.get("type", "sparse")
    config = BNAConfig.with_defaults()
    if scenario_type == "complete":
        _clear_structural(config)
        _apply_layer(config, meta.get("config", {}) or {}, {}, label)
        _apply_rule_sets(config, meta.get("rule_sets", {}) or {}, label)
    else:
        layer = {
            k: v for k, v in meta.items()
            if k not in ("description", "type", "version", "base")
        }
        _apply_layer(config, layer, {}, label)
    return config


def _scenario_meta(scenario: str | dict | Path) -> tuple[dict, str]:
    """Resolve a scenario reference to ``(meta_doc, provenance_label)``.

    * ``dict``  → used directly (an inline scenario document).
    * ``Path``  → parsed from that YAML file.
    * ``str``   → a **bundled** scenario name (optionally ``name@version``); user
                  scenarios are a workspace concept resolved by the app, not here.
    """
    import yaml as _yaml

    from bikescore.scenarios import _bundled_scenarios_dir

    if isinstance(scenario, dict):
        return scenario, "scenario:inline"
    if isinstance(scenario, Path):
        meta = _yaml.safe_load(scenario.read_text(encoding="utf-8")) or {}
        return meta, f"scenario:{scenario.stem}"
    if isinstance(scenario, str):
        name, _, version = scenario.partition("@")
        fname = f"{name}@{version}.yaml" if version else f"{name}.yaml"
        path = _bundled_scenarios_dir() / fname
        try:
            meta = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raise ConfigResolverError(f"Bundled scenario {scenario!r} not found.")
        return meta, f"scenario:{name}"
    raise ConfigResolverError(
        f"scenario must be None, a name, a dict, or a Path (got {type(scenario).__name__})."
    )


def build_config(
    scenario: str | dict | Path | None = None,
    overrides: dict | None = None,
) -> BNAConfig:
    """Build an effective ``BNAConfig`` from a scenario, DB-free (Phase 38b).

    Args:
        scenario: ``None`` → ``BNAConfig.with_defaults()``; a ``str`` bundled scenario
            name (e.g. ``"default"``, ``"name@2"``); an inline scenario ``dict``; or a
            ``Path`` to a scenario YAML file.
        overrides: optional ``{dotted_key: value}`` scalar one-offs (``--set`` form),
            applied last (e.g. ``{"city.default_speed": 40}``).

    Returns:
        The resolved ``BNAConfig`` — no workspace, no provenance, no city layering.
    """
    if scenario is None:
        from bikescore.config import BNAConfig

        config = BNAConfig.with_defaults()
    else:
        meta, label = _scenario_meta(scenario)
        config = _config_from_meta(meta, label)
    if overrides:
        _apply_set(config, overrides, {})
    return config


def list_bundled_scenarios() -> list[str]:
    """Return the names of bundled scenarios (unversioned aliases only)."""
    from bikescore.scenarios import list_bundled_scenarios as _list

    return _list()
