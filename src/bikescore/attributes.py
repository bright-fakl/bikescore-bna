"""Custom attribute extensibility — user-defined computed columns for ways_df."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from bikescore.decision.model import Decision


@dataclass
class CustomAttribute:
    """A user-defined computed column added to ways_df at a specified pipeline stage.

    The compute function must be vectorized (takes the full DataFrame, returns a Series).
    Row-by-row iteration is not acceptable — DC has 98k segments.

    Once added, the column is available in any RuleSet condition by name.

    Args:
        name: Column name added to ways_df. Must not conflict with standard BNA columns.
        dtype: Expected Python type for validation.
        compute: Vectorized function: DataFrame -> Series.
        description: Human-readable explanation of what this attribute captures.
        extra_tags: OSM tags this attribute reads from ways_df.
        after: Ordering deps (attribute names that must run before this one).
        version: Bump when compute logic changes (used for hashing since Python
            callables can't be hashed by content).

    Example:
        CustomAttribute(
            name="is_school_zone",
            dtype=bool,
            compute=lambda df: df["tags"].str.get("school:zone") == "yes",
        )
    """

    name: str
    dtype: type
    compute: Callable[[pd.DataFrame], pd.Series]
    description: str = ""
    extra_tags: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    version: str = "1.0"

    def __post_init__(self) -> None:
        if isinstance(self.extra_tags, list):
            object.__setattr__(self, "extra_tags", tuple(self.extra_tags))
        if isinstance(self.after, list):
            object.__setattr__(self, "after", tuple(self.after))

    def apply(self, df: pd.DataFrame, vars: dict | None = None) -> pd.DataFrame:
        """Apply this attribute's compute function to df, returning df with the new column."""
        df = df.copy()
        df[self.name] = self.compute(df)
        return df

    def output_columns(self) -> set[str]:
        return {self.name}

    def referenced_input_columns(self) -> set[str]:
        return set()

    def referenced_variables(self) -> set[str]:
        return set()


@dataclass
class DecisionAttribute:
    """A YAML-declared attribute that uses the decision DSL to compute columns.

    Unlike CustomAttribute (Python callable), DecisionAttribute is fully serializable
    and can be loaded from a YAML bikescore.yaml config file.

    Args:
        name: Identifier for this attribute (must be unique in the registry).
        compute: A Decision object that defines the computation logic.
        extra_tags: OSM tags this attribute reads that are not in WAY_TAGS.
        after: Ordering deps (attribute names that must run before this one).
        description: Human-readable explanation.
    """

    name: str
    compute: Decision
    extra_tags: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    description: str = ""
    fallback: Decision | None = None
    persist: bool = True
    # Phase 36a: the *authored* terse docs, retained verbatim from ``from_dict`` so
    # ``to_dict`` re-emits the compact source (``for:`` sweeps, named ``sets:``, ``map:``)
    # rather than a re-rendered flat form. ``None`` for code-built attributes (the
    # ``decision_to_terse`` importer renders one). Excluded from equality + hashing
    # (``compare=False``); hashing keys on the compiled canonical ``compute``/``fallback``.
    compute_doc: dict | None = field(default=None, repr=False, compare=False)
    fallback_doc: dict | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.extra_tags, list):
            object.__setattr__(self, "extra_tags", tuple(self.extra_tags))
        if isinstance(self.after, list):
            object.__setattr__(self, "after", tuple(self.after))

    def apply(self, df: pd.DataFrame, vars: dict | None = None) -> pd.DataFrame:
        """Apply this attribute's *observed* Decision, returning df with new columns.

        Decision.apply() returns the full modified DataFrame. We restrict to the
        observed compute's output fields so only the attribute's declared outputs are
        written back. Fallback passes (if any) run separately via :meth:`apply_fallback`
        AFTER all observed attributes and the stage's class-adjustment step, because the
        FC-default fallbacks key on the *adjusted* functional_class (Phase 35f).
        """
        result = self.compute.apply(df, vars or {})
        df = df.copy()
        for col in self.compute.output_fields():
            df[col] = result[col]
        return df

    def apply_fallback(self, df: pd.DataFrame, vars: dict | None = None) -> pd.DataFrame:
        """Apply the attribute's ordered fallback passes (Phase 35f), if declared.

        Fallback passes fill only still-NULL rows they match (each rule guards on
        ``{col}: is_null``), turning imputation into the lower-priority tail of the
        attribute's decision: ``observed → fallback(s) → (default)``. For every column a
        fallback pass writes, a companion boolean ``{col}_imputed`` is auto-generated,
        True exactly where a fallback pass produced the value (NULL before the fallback,
        non-NULL after) — provenance that a value came from a config/FC default rather
        than an OSM tag. Attributes with no fallback get no companion column.
        """
        if self.fallback is None:
            return df
        df = df.copy()
        targets = list(self.fallback.output_fields())
        null_before = {
            c: (df[c].isna() if c in df.columns
                else pd.Series(True, index=df.index))
            for c in targets
        }
        result = self.fallback.apply(df, vars or {})
        for col in targets:
            df[col] = result[col]
        for col in targets:
            df[f"{col}_imputed"] = (null_before[col] & df[col].notna())
        return df

    def fallback_output_columns(self) -> set[str]:
        """Columns the fallback passes write (each gets an auto ``{col}_imputed``)."""
        return set(self.fallback.output_fields()) if self.fallback is not None else set()

    def output_columns(self) -> set[str]:
        cols = set(self.compute.output_fields())
        if self.fallback is not None:
            fb = set(self.fallback.output_fields())
            cols |= fb
            cols |= {f"{c}_imputed" for c in fb}
        return cols

    def referenced_input_columns(self) -> set[str]:
        cols = self.compute.referenced_fields()
        if self.fallback is not None:
            cols |= self.fallback.referenced_fields()
        return cols

    def referenced_variables(self) -> set[str]:
        """Config ``$var:`` names this attribute's compute + fallback reference."""
        names = self.compute.referenced_variables()
        if self.fallback is not None:
            names |= self.fallback.referenced_variables()
        return names

    def to_dict(self, *, canonical: bool = False) -> dict:
        """Serialize the attribute (Phase 36a — store authored terse verbatim).

        Emits the *authored* terse docs (``compute_doc``/``fallback_doc``) unchanged so a
        save→reload→save round-trip keeps the compact authoring constructs (``map:``,
        ``for:`` sweeps, named ``sets:``) instead of a flattened re-render. When no doc
        was retained — a code-built attribute constructed from a ``Decision`` — the
        ``decision_to_terse`` *importer* renders an initial terse form.

        ``canonical=True`` forces the canonical-derived render regardless of any stored
        doc; the hashing path (``_serialize_attribute``) uses it so the attribute hash
        stays keyed on the compiled canonical, independent of storage form.
        """
        from bikescore.decision import decision_to_terse
        d = {
            "name": self.name,
            "extra_tags": list(self.extra_tags),
            "after": list(self.after),
            "description": self.description,
            "compute": (
                decision_to_terse(self.compute)
                if canonical or self.compute_doc is None
                else self.compute_doc
            ),
        }
        if self.fallback is not None:
            # Imputation tail (Phase 35f): lower-priority FC/config-default passes.
            d["fallback"] = (
                decision_to_terse(self.fallback)
                if canonical or self.fallback_doc is None
                else self.fallback_doc
            )
        if not self.persist:
            # Phase 35n: non-persisted scratch attribute (dropped before stage write).
            d["persist"] = False
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DecisionAttribute:
        from bikescore.decision import Decision, decision_from_doc
        # Phase 36a: retain the authored doc verbatim (``compute_doc``/``fallback_doc``)
        # so ``to_dict`` re-emits the exact source. ``decision_from_doc`` compiles either
        # the canonical ``{name, passes:[{table}]}`` form or the terse authored form
        # losslessly into the runtime/hash ``Decision``.
        compute_raw = d.get("compute", {})
        if isinstance(compute_raw, Decision):
            compute = compute_raw
        else:
            compute = decision_from_doc(compute_raw)
        fallback_raw = d.get("fallback")
        if fallback_raw is None:
            fallback = None
        elif isinstance(fallback_raw, Decision):
            fallback = fallback_raw
        else:
            fallback = decision_from_doc(fallback_raw)
        return cls(
            name=str(d["name"]),
            compute=compute,
            extra_tags=tuple(d.get("extra_tags", [])),
            after=tuple(d.get("after", [])),
            description=str(d.get("description", "")),
            fallback=fallback,
            persist=bool(d.get("persist", True)),
            compute_doc=compute_raw if isinstance(compute_raw, dict) else None,
            fallback_doc=fallback_raw if isinstance(fallback_raw, dict) else None,
        )


@dataclass
class PrimaryAttribute:
    """A raw OSM-tag attribute: load ``osm_tag`` directly as a column, no decision.

    The thinnest attribute kind (Phase 35g). It owns a single raw OSM tag — Parse
    loads ``osm_tag`` because the attribute contributes it to
    :meth:`AttributeRegistry.extra_osm_tags`, and the attribute *produces* the ``name``
    column (so it participates in topo ordering and producer/consumer validation like
    any other attribute, M6). When ``name == osm_tag`` (the common case) the column is
    the raw tag Parse already wrote, so :meth:`apply` is a no-op; when they differ the
    raw tag is copied into ``name``.

    Unlike :class:`DecisionAttribute` there is no ``compute`` — a primary attribute is
    "load this tag", nothing more. Migrating a tag out of the hard-coded
    ``parse.BASE_WAY_TAGS`` routing core into a primary attribute keeps Parse's effective
    tag set unchanged while making the load explicit and replaceable.

    Args:
        name: Column added to ways_df. Defaults to ``osm_tag`` when omitted.
        osm_tag: The raw OSM tag Parse loads.
        after: Intra-stage ordering deps (attribute names that must run before this one).
        description: Human-readable explanation.
    """

    name: str
    osm_tag: str
    after: tuple[str, ...] = ()
    description: str = ""
    persist: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.after, list):
            object.__setattr__(self, "after", tuple(self.after))

    @property
    def extra_tags(self) -> tuple[str, ...]:
        """Parse loads this so the column exists; the raw tag is the only input."""
        return (self.osm_tag,)

    def apply(self, df: pd.DataFrame, vars: dict | None = None) -> pd.DataFrame:
        """Materialize the ``name`` column from the raw ``osm_tag``.

        No-op when ``name == osm_tag`` (Parse already wrote that column); otherwise
        copy the raw tag column (or NULL if Parse did not load it) into ``name``.
        """
        if self.name == self.osm_tag:
            return df
        df = df.copy()
        df[self.name] = df[self.osm_tag] if self.osm_tag in df.columns else pd.NA
        return df

    def apply_fallback(self, df: pd.DataFrame, vars: dict | None = None) -> pd.DataFrame:
        """Primary attributes have no imputation tail."""
        return df

    def fallback_output_columns(self) -> set[str]:
        return set()

    def output_columns(self) -> set[str]:
        return {self.name}

    def referenced_input_columns(self) -> set[str]:
        return set()

    def referenced_variables(self) -> set[str]:
        return set()

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "osm_tag": self.osm_tag,
            "after": list(self.after),
            "description": self.description,
        }
        if not self.persist:
            d["persist"] = False
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PrimaryAttribute:
        osm_tag = str(d["osm_tag"])
        return cls(
            name=str(d.get("name", osm_tag)),
            osm_tag=osm_tag,
            after=tuple(d.get("after", [])),
            description=str(d.get("description", "")),
            persist=bool(d.get("persist", True)),
        )


# Type union for all attribute kinds
Attribute = CustomAttribute | DecisionAttribute | PrimaryAttribute


def attribute_from_dict(d: dict) -> Attribute:
    """Deserialize one attribute entry, dispatching on shape (Phase 35g).

    An entry carrying ``osm_tag`` is a :class:`PrimaryAttribute` (raw-tag load, no
    decision); anything else is a :class:`DecisionAttribute` (``compute`` Decision).
    Used by every list-loading path (built-ins, scenario resolver, CLI, web) so both
    kinds round-trip.
    """
    if "osm_tag" in d:
        return PrimaryAttribute.from_dict(d)
    return DecisionAttribute.from_dict(d)


# Standard BNA columns that Attribute names must not shadow
_RESERVED_COLUMNS = frozenset({
    "road_id", "osm_id", "geometry", "name", "highway", "length_m",
    "speed_limit", "ft_lanes", "tf_lanes", "width_ft", "one_way",
    "ft_bike_infra", "tf_bike_infra", "ft_park", "tf_park",
    "segment_id", "start_node_id", "end_node_id",
    "ft_seg_stress", "tf_seg_stress", "ft_int_stress", "tf_int_stress",
    "tags", "node_ids",
})

# Columns provided to attributes by the stage itself rather than another attribute.
# Empty since Phase 35n made functional_class an ordinary attribute; kept as the seam
# for any future stage-provided input.
_STAGE_PROVIDED_COLUMNS: frozenset[str] = frozenset()


def _get_base_way_tags() -> frozenset[str]:
    """Return BASE_WAY_TAGS from parse.py as the base set of known OSM tag columns."""
    from bikescore.stages.parse import BASE_WAY_TAGS
    return frozenset(BASE_WAY_TAGS)


def _serialize_attribute(f: Attribute) -> dict:
    # Hash serialization (Phase 36a): a DecisionAttribute hashes on its canonical-derived
    # form (``canonical=True``) so the attribute hash is keyed on the compiled decision,
    # not the stored authored doc — two storage forms that compile identically hash alike.
    if isinstance(f, DecisionAttribute):
        return f.to_dict(canonical=True)
    if isinstance(f, PrimaryAttribute):
        return f.to_dict()
    else:
        return {
            "name": f.name,
            "extra_tags": list(f.extra_tags),
            "version": f.version,
        }


def load_builtin_attributes() -> list[Attribute]:
    """Load built-in attributes from data/attributes/standard-bna.yaml."""
    from pathlib import Path

    import yaml
    path = Path(__file__).parent / "data" / "attributes" / "standard-bna.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [attribute_from_dict(d) for d in raw.get("attributes", [])]


class AttributeRegistry:
    """Manages custom attributes (CustomAttribute and DecisionAttribute) added to ways_df.

    Default instance contains no attributes — only user-registered attributes live here.
    Standard BNA attributes (functional_class, bike_infra, etc.) are loaded as
    built-in DecisionAttributes and applied by the Attributes stage (Phase 34i).
    """

    def __init__(self) -> None:
        self._attributes: dict[str, Attribute] = {}

    def register(self, attribute: Attribute) -> None:
        """Register a attribute.

        Raises:
            ValueError: if name conflicts with a reserved column, is already registered,
                outputs conflict with existing DecisionAttribute outputs, or referenced
                fields are not covered by extra_tags.
        """
        if attribute.name in _RESERVED_COLUMNS:
            raise ValueError(
                f"Attribute name '{attribute.name}' conflicts with a standard "
                f"BNA column. Choose a different name."
            )
        if attribute.name in self._attributes:
            raise ValueError(
                f"Attribute '{attribute.name}' already registered. Use replace()."
            )
        if isinstance(attribute, DecisionAttribute):
            new_outputs = attribute.output_columns()
            for existing in self._attributes.values():
                if isinstance(existing, DecisionAttribute):
                    overlap = new_outputs & existing.output_columns()
                    if overlap:
                        raise ValueError(
                            f"Attribute '{attribute.name}' sets columns already claimed "
                            f"by '{existing.name}': {sorted(overlap)}"
                        )
            base_tags = _get_base_way_tags()
            # Only the observed compute is topo-ordered at registration; fallback refs run
            # in the late batched phase (any attribute's output is available) and are
            # validated by resolve-time producer/consumer analysis (M6), not here.
            referenced = attribute.compute.referenced_fields()
            # Allow computed columns from prior attributes (cross-attribute deps)
            # and self-outputs from earlier passes (intra-attribute multi-pass deps)
            known_computed: set[str] = set()
            for existing in self._attributes.values():
                known_computed |= existing.output_columns()
            known_computed |= attribute.output_columns()
            missing = (
                referenced - base_tags - set(attribute.extra_tags)
                - known_computed - _STAGE_PROVIDED_COLUMNS
            )
            if missing:
                raise ValueError(
                    f"Attribute '{attribute.name}' references columns {sorted(missing)} "
                    "not in BASE_WAY_TAGS, extra_tags, or prior attribute outputs. "
                    "Declare OSM tags in extra_tags."
                )
        self._attributes[attribute.name] = attribute

    def replace(self, name: str, attribute: Attribute) -> None:
        """Replace an existing attribute."""
        if name not in self._attributes:
            raise KeyError(f"Attribute '{name}' not found")
        self._attributes[name] = attribute

    def disable(self, name: str) -> None:
        """Remove a attribute by name."""
        self._attributes.pop(name, None)

    def get(self, name: str) -> Attribute | None:
        """Return the named attribute, or None if not registered."""
        return self._attributes.get(name)

    def in_topo_order(self) -> list[Attribute]:
        """Return all registered attributes in topological order over `after:` deps.

        A single global sort (no stage/gate partitioning): each attribute lists in
        ``after`` the attribute names that must run before it; independent attributes
        are ordered by name for determinism. The Attributes stage applies this one
        ordered list (Phase 34i).
        """
        attributes = {f.name: f for f in self._attributes.values()}
        if not attributes:
            return []
        deps: dict[str, set[str]] = {
            name: (set(f.after) & attributes.keys())
            for name, f in attributes.items()
        }
        in_degree = {name: len(d) for name, d in deps.items()}
        dependents: dict[str, list[str]] = {name: [] for name in attributes}
        for name, d in deps.items():
            for dep in d:
                dependents[dep].append(name)
        queue = [name for name, deg in in_degree.items() if deg == 0]
        queue.sort()
        result = []
        while queue:
            name = queue.pop(0)
            result.append(attributes[name])
            for dep in sorted(dependents[name]):
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)
                    queue.sort()
        if len(result) != len(attributes):
            raise ValueError("Cycle detected in attribute `after:` dependencies")
        return result

    def extra_osm_tags(self) -> set[str]:
        """Return the union of all extra_tags declared across all attributes."""
        result: set[str] = set()
        for f in self._attributes.values():
            result.update(f.extra_tags)
        return result

    def non_persisted_columns(self) -> set[str]:
        """Columns produced by ``persist: false`` attributes (Phase 35n).

        These are computed and available to in-stage consumers (e.g. functional_class
        reads the cls_* flags) but are dropped before the stage writes its parquet, so
        they never reach downstream stages or the output schema.
        """
        cols: set[str] = set()
        for f in self._attributes.values():
            if not getattr(f, "persist", True):
                cols |= f.output_columns()
        return cols

    def apply_all(
        self, df: pd.DataFrame, vars: dict | None = None
    ) -> pd.DataFrame:
        """Apply all registered attributes' *observed* computes in topo order (34i)."""
        attributes = self.in_topo_order()
        if not attributes:
            return df
        for attribute in attributes:
            df = attribute.apply(df, vars or {})
        return df

    def apply_all_fallbacks(
        self, df: pd.DataFrame, vars: dict | None = None
    ) -> pd.DataFrame:
        """Apply every attribute's fallback passes + auto ``{col}_imputed`` (Phase 35f).

        Run by the Attributes stage *after* :meth:`apply_all` and the class-adjustment
        step, so FC-default fallbacks key on the promoted functional_class. Attributes
        with no fallback are no-ops, so no unused ``_imputed`` companions are created.
        """
        attributes = self.in_topo_order()
        for attribute in attributes:
            if isinstance(attribute, DecisionAttribute) and attribute.fallback is not None:
                df = attribute.apply_fallback(df, vars or {})
        return df

    def to_dict(self) -> list[dict]:
        """Stable serialization for hashing, sorted by name (Phase 34i)."""
        attributes = sorted(self._attributes.values(), key=lambda f: f.name)
        return [_serialize_attribute(f) for f in attributes]

    def __len__(self) -> int:
        return len(self._attributes)
