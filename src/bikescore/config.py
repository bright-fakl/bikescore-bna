"""BNA pipeline configuration schema."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class ConfigValidationError(ValueError):
    """Raised when an effective config is internally inconsistent.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers around
    config validation keep working.
    """


# ── Sub-configs ───────────────────────────────────────────────────────────────

@dataclass
class CityIdentityConfig:
    """City-level parameters consumed by multiple stages."""

    default_speed: int | None = None
    """City-wide default speed in mph for residential roads.
    Applied before state default and FC fallback in speed imputation.
    """

    state_default_speed: int | None = None
    """State-wide default speed in mph for residential roads.
    Applied after city default but before FC fallback.
    """

    country: str = "us"
    """ISO 3166-1 alpha-2 country code. Determines bare-speed-unit convention
    (km/h everywhere except us/gb/mm/lr where mph is assumed).
    """


@dataclass
class ImputationConfig:
    """Controls how missing road attributes are filled before stress computation.

    Speed defaults are resolved in priority order:
    1. OSM maxspeed tag (parsed, converted to mph)
    2. city.default_speed   — residential roads only (COALESCE(:city_default) in SQL)
    3. city.state_default_speed — residential roads only (COALESCE(:state_default) in SQL)
    4. Per-functional-class default — all remaining NULLs (primary=40, secondary=40,
       tertiary=30, unclassified=25, etc.). These FC defaults are the fallback passes of
       the ``speed_parsed`` attribute (Phase 35f); customise by editing the scenario's
       speed attribute. All speed values are stored in mph throughout the pipeline.
    """

    bare_speed_unit: str = "km/h"
    """Unit assumed for OSM maxspeed values with no unit suffix (e.g. "50").

    OSM convention: km/h in most countries; mph in US, UK, Myanmar, Liberia.
    Set to "mph" for cities in those countries so bare numbers are used as-is
    instead of being divided by 1.609. The Pipeline sets this automatically
    from City.country; override in TOML only for non-standard cases.
    """

    default_facility_width_ft: float = 5.0
    """Fallback bike-infra facility width in feet, used when ft/tf_bike_infra_width is
    null. Matches :default_facility_width in stress_segments_higher_order.sql."""

@dataclass
class StressConfig:
    """Controls stress rule application.

    n_levels is a derived property — the maximum stress value any rule produces.
    There is no explicit n_levels field. Adding a rule that produces stress=4
    automatically makes this a 4-level model.

    The BNA default rules only produce values 1 and 3 (level 2 is never assigned
    in the SQL reference despite the column allowing it).
    """

    segment_rules: Any = field(default=None)
    intersection_rules: Any = field(default=None)

    crossing_speed_defaults: dict[str, int] = field(
        default_factory=lambda: {
            "primary_speed": 40, "primary_lanes": 2,
            "secondary_speed": 40, "secondary_lanes": 2,
            "tertiary_speed": 30, "tertiary_lanes": 1,
        }
    )
    """Default crossing-road speed/lane values used in the intersection-stress EXISTS
    checks when the crossing road has no maxspeed/lanes tag. Consumed by the
    ``intersection_context`` provider (rules/providers.py). Mirrors the per-FC
    defaults in stress_lesser_ints.sql / stress_tertiary_ints.sql.
    """

    def __post_init__(self) -> None:
        from bikescore.rules.stress_intersection import default_intersection_stress_rules
        from bikescore.rules.stress_segment import default_segment_stress_rules
        if self.segment_rules is None:
            self.segment_rules = default_segment_stress_rules()
        if self.intersection_rules is None:
            self.intersection_rules = default_intersection_stress_rules()

    level_names: dict[int, str] = field(default_factory=dict)
    """Optional human-readable names per stress level.

    Empty = auto-label as "level_1", "level_2", etc.
    For LTS 4: {1: "LTS1", 2: "LTS2", 3: "LTS3", 4: "LTS4"}
    """

    @property
    def n_levels(self) -> int:
        """Maximum stress value produced by any rule. Inferred from rules, not configured."""
        if self.segment_rules is None and self.intersection_rules is None:
            return 3  # BNA default
        seg_max = (
            self.segment_rules.max_output_value(["ft_seg_stress", "tf_seg_stress"])
            if self.segment_rules is not None else 1
        )
        int_max = (
            self.intersection_rules.max_output_value(["ft_int_stress", "tf_int_stress"])
            if self.intersection_rules is not None else 1
        )
        return max(seg_max, int_max, 1)


@dataclass
class GraphConfig:
    """Controls routing graph construction."""

    low_stress_threshold: int = 1
    """Edges with link_stress <= this value go into G_low.

    BNA default = 1: only stress-level-1 roads are "low stress".
    Set to 2 for LTS 1+2 network (comfortable for most adults).
    Validated on startup: must be <= StressConfig.n_levels.
    """

    extra_thresholds: list[int] = field(default_factory=list)
    """Build additional low-stress graphs at these thresholds.

    Produces extra low_stress_cost_N columns in connectivity_df.
    Default [] = BNA-compatible single-threshold output.
    """

    link_stress_model: str = "max"
    """Aggregation model for a link's stress from its source-segment,
    intersection, and target-segment stresses.

    - "max": BNA default — max(source, intersection, target).
    - "segment_only": ignore intersection stress — max(source, target).
    - "sum": additive, capped at stress.n_levels.
    """

    def __post_init__(self) -> None:
        valid = {"max", "segment_only", "sum"}
        if self.link_stress_model not in valid:
            raise ValueError(
                f"graph.link_stress_model must be one of {sorted(valid)}, "
                f"got {self.link_stress_model!r}"
            )


@dataclass
class ConnectivityConfig:
    """Controls block-to-block connectivity computation."""

    include_self_pairs: bool = True
    """Include (block, block, cost=0) self-pairs. Matches SQL baseline behavior."""

    use_turn_restrictions: bool = False
    """Apply OSM turn restrictions during Dijkstra. Not in SQL reference — opt-in."""

    batches_per_worker: int = 4
    """Dijkstra partitions per CPU thread. Higher = more frequent progress updates."""

    n_workers: int | None = None
    """Number of parallel workers. None = auto-detect from CPU count."""

    low_stress_ratio: float = 1.25
    """A block pair is flagged low_stress=True if ls_cost / hs_cost <= this ratio.

    Also True if source and target blocks share any road_id (adjacent blocks).
    Currently hardcoded at 1.25 in the SQL reference; exposed for research use.
    """


@dataclass
class PopulationScoringParams:
    """Piecewise ratio formula parameters for population access scoring."""

    max_score: float = 1.0
    step1: float = 0.03
    score1: float = 0.1
    step2: float = 0.2
    score2: float = 0.4
    step3: float = 0.5
    score3: float = 0.8


@dataclass
class ScoringConfig:
    """Controls access score computation.

    ``people`` (population access) is a fixed top-level weight; it is not
    destination-based. All destination-category weights live in the open
    ``category_weights`` dict, keyed by scoring category. The set of valid
    categories is determined by the active ``DestinationRegistry`` (each
    ``DestinationType`` declares its ``scoring_category`` and ``category_weight``);
    nothing about category membership or intra-category weights is hard-coded here.

    ``people`` + ``sum(category_weights.values())`` must equal 100.
    """

    people: int = 15  # population access — not destination-based, stays fixed
    category_weights: dict[str, int] = field(
        default_factory=lambda: {
            "opportunity": 20,
            "core_services": 20,
            "retail": 15,
            "recreation": 15,
            "transit": 15,
        }
    )

    population: PopulationScoringParams = field(default_factory=PopulationScoringParams)

    def validate(self) -> None:
        """Raise ValueError if weights don't sum to 100."""
        total = self.people + sum(self.category_weights.values())
        if total != 100:
            raise ValueError(
                f"scoring weights sum to {total}, must be 100 "
                f"(people={self.people}, categories={self.category_weights})"
            )


def validate_scoring_categories(scoring: ScoringConfig, destinations: Any) -> None:
    """Raise if any active destination type's scoring_category lacks a weight.

    Membership and intra-category weights come from the active
    ``DestinationRegistry``; the inter-category weight for each category must be
    declared in ``scoring.category_weights``. A destination whose
    ``scoring_category`` has no matching entry would otherwise be silently
    dropped from the overall score.
    """
    if destinations is None:
        return
    weights = scoring.category_weights
    for dt in destinations.active():
        cat = dt.scoring_category
        if cat not in weights:
            raise ConfigValidationError(
                f"unknown category '{cat}' — add it to scoring.category_weights"
            )


@dataclass
class ExportConfig:
    """Controls output file generation."""

    base_dir: Path = field(default_factory=lambda: Path("./results"))
    """Root directory for calver-versioned output: base_dir/country/region/city/YY.MM/"""


@dataclass
class CacheConfig:
    """Filesystem cache location for the core.

    The only field the scoring core reads is ``cache_dir`` (where ``parse`` caches the
    clipped regional PBF). Run-store / GC policy — TTL, size caps, orphan collection — is
    the orchestration layer's concern and lives there, not here.
    """

    cache_dir: Path = field(default_factory=lambda: Path("./cache"))


# ── Top-level config ──────────────────────────────────────────────────────────

@dataclass
class BNAConfig:
    """Complete configuration for one BNA pipeline run.

    Global parameters are used by multiple stages and live at the top level.
    Per-stage parameters live in their respective sub-config objects.
    The two extensibility registries (attributes, destinations) are also here.
    """

    # ── Global parameters (used by multiple stages) ───────────────────────────

    max_trip_distance: int = 2680
    """Dijkstra distance cutoff in metres (~15 min cycling). Used by graph + connectivity."""

    block_road_buffer: int = 15
    """Buffer radius in metres for associating roads with census blocks.
    Replicates ST_Buffer(block.geom, 15) in census_blocks.sql.
    """

    block_road_min_length: int = 30
    """Minimum road-buffer overlap length in metres to associate a road with a block.
    Replicates ST_Length(ST_Intersection(buffer, road)) > 30 in census_blocks.sql.
    """

    block_boundary_overlap: float = 0.50
    """Minimum fraction of a census block's area that must fall inside the city
    boundary for the block to be retained. Replicates the 0.50 overlap test used
    when clipping blocks to the analysis boundary in graph._build_blocks.
    """

    exclude_water_blocks: bool = True
    """Drop census blocks with zero land area (aland20 == 0, i.e. all-water blocks).
    Replicates the ALAND20 > 0 filter in census_blocks.sql.
    """

    output_srid: int | None = None
    """Projected CRS (EPSG code) for distance calculations in metres.
    None = auto-detect using geopandas.GeoSeries.estimate_utm_crs().
    Set explicitly for reproducibility (DC = 32618, most of US East = 32618).
    """

    min_path_length: int = 4800
    """Minimum trail path length in metres for recreational trail scoring."""

    min_bbox_length: int = 3300
    """Minimum trail bounding-box diagonal in metres (filters looping paths)."""

    # ── Extensibility registries ──────────────────────────────────────────────

    attributes: Any = field(default=None)
    """AttributeRegistry: custom computed columns added to ways_df."""

    destinations: Any = field(default=None)
    """DestinationRegistry: active destination types with OSM matchers and scoring params.
    None here — set via BNAConfig.with_defaults() to avoid circular import.
    """

    intersection_attributes: Any = field(default=None)
    """list[IntersectionAttribute]: named boolean node attributes (signalized, stop,
    rrfb, island) computed from OSM node tags by the parse stage and consumed by the
    intersection-stress model. None here — seeded via BNAConfig.with_defaults()."""

    variables: dict[str, Any] = field(default_factory=dict)
    """User-defined config variables available as ``$var:`` references in all DSL
    contexts — stress/classification/imputation rules and DecisionAttributes. Merged
    into the rule/attribute ``variables`` dict at each call site (stage-declared
    variables win on name collision). Scenario- and city-settable under a top-level
    ``variables:`` section."""

    required_variables: set[str] = field(default_factory=set)
    """Names declared ``required: true`` in the ``variables:`` mapping form (Phase 36b).
    A required variable that resolves to ``None`` at run-time fails
    ``validate(runtime=True)``. Not a hash input — a validation contract, not a
    computation field."""

    # ── Project layout ────────────────────────────────────────────────────────

    project_root: Path = field(default_factory=lambda: Path.home() / ".bikescore" / "projects")
    """Root directory for per-city project folders.

    Each city lives at project_root / slug / containing city.toml, data/,
    stages/, runs/, exports/, and optional rules/.  Falls back to cache_dir /
    slug when the project directory does not exist.
    """

    # ── Per-stage config ──────────────────────────────────────────────────────

    city: CityIdentityConfig = field(default_factory=CityIdentityConfig)
    imputation: ImputationConfig = field(default_factory=ImputationConfig)
    stress: StressConfig = field(default_factory=StressConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    connectivity: ConnectivityConfig = field(default_factory=ConnectivityConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)

    def validate(self, *, runtime: bool = False) -> None:
        """Validate cross-config constraints.

        ``runtime=True`` additionally enforces run-time-only contracts (the
        ``required`` variable check, Phase 36b) — used at the engine run-prep
        chokepoint where the city is layered in and the resolved value is known.
        A city-free scenario save uses the default (``runtime=False``)."""
        self.scoring.validate()
        validate_scoring_categories(self.scoring, self.destinations)
        # The intersection-stress model (rules/providers.py) hard-codes the four BNA
        # attribute columns. Until it is generalized (Decision 4 Phase 2), the active
        # intersection attributes must include them by name — fail fast here rather
        # than KeyError deep in the stress stage. None = defaults seeded at runtime.
        if self.intersection_attributes is not None:
            from bikescore.intersection_attributes import STRESS_REQUIRED_ATTRIBUTES
            active = {a.name for a in self.intersection_attributes if a.enabled}
            missing = STRESS_REQUIRED_ATTRIBUTES - active
            if missing:
                raise ConfigValidationError(
                    "intersection_attributes is missing names required by the "
                    f"intersection-stress model: {sorted(missing)}. The four BNA "
                    "attributes (signalized, stop, rrfb, island) may be customized "
                    "but not removed or renamed until the stress model is generalized."
                )
        # Producer/consumer column check (Phase 35i, M6): every column an active
        # rule (or a hard-coded stage consumer) references must be produced by an
        # active attribute, a ruleset output, a base OSM tag, or a stage. Fail at
        # save/resolve rather than KeyError-ing or falling to a default deep in a
        # stage. Skip when attributes are unseeded (None) — producers are unknown
        # until with_defaults() runs; resolved scenario/city configs always have them.
        if self.attributes is not None:
            from bikescore.decision.analysis.producer_consumer import (
                unproduced_references,
            )
            misses = unproduced_references(self)
            if misses:
                detail = "; ".join(
                    f"{col!r} (referenced by {src} rule {rid!r})"
                    for col, src, rid in sorted(misses)
                )
                raise ConfigValidationError(
                    "Rules reference columns no active attribute or ruleset "
                    f"produces: {detail}. Add a producing attribute or fix the rule."
                )
            # Undeclared-$var: gate (Phase 35h, M3): every $var: an active rule or
            # attribute references must be declared in config.variables (this scenario's
            # own) or provided by a stage (_RULE_VARIABLES). Scenarios are self-contained
            # — they may not lean on a variable only a city defines. City-free scenario
            # resolution makes config.variables the scenario's own declarations here; at
            # run-time (city layered in) this stays a correct backstop.
            from bikescore.decision.analysis.variables import undeclared_variables
            undeclared = undeclared_variables(self)
            if undeclared:
                detail = "; ".join(
                    f"$var:{name} (referenced by {', '.join(srcs)})"
                    for name, srcs in sorted(undeclared.items())
                )
                raise ConfigValidationError(
                    "Rules/attributes reference undeclared variables: "
                    f"{detail}. Declare them under this scenario's variables, or "
                    "remove the reference."
                )
        # Required-variable contract (Phase 36b): a variable declared ``required: true``
        # must resolve to a non-null value. Enforced only at run-time (city layered in) —
        # a city-free scenario save legitimately leaves a required var unset for the city.
        if runtime and self.required_variables:
            missing = sorted(
                n for n in self.required_variables if self.variables.get(n) is None
            )
            if missing:
                raise ConfigValidationError(
                    "Required variables are unset (null): "
                    f"{', '.join(missing)}. Provide a value (e.g. as a city override)."
                )
        if self.graph.low_stress_threshold > self.stress.n_levels:
            raise ValueError(
                f"graph.low_stress_threshold ({self.graph.low_stress_threshold}) "
                f"exceeds stress.n_levels ({self.stress.n_levels})"
            )
        for t in self.graph.extra_thresholds:
            if t > self.stress.n_levels:
                raise ValueError(
                    f"graph.extra_thresholds contains {t} which exceeds "
                    f"stress.n_levels ({self.stress.n_levels})"
                )

    @classmethod
    def with_defaults(cls, **kwargs: object) -> BNAConfig:
        """Create a BNAConfig with fully initialized registries and default rules.

        Preferred over BNAConfig() directly when running the full pipeline,
        as it wires up the DestinationRegistry and AttributeRegistry with built-in attributes.
        """
        from bikescore.attributes import AttributeRegistry, load_builtin_attributes
        from bikescore.destinations import default_destination_registry
        from bikescore.intersection_attributes import default_intersection_attributes
        config = cls(**kwargs)
        if config.destinations is None:
            config.destinations = default_destination_registry()
        if config.attributes is None:
            config.attributes = AttributeRegistry()
            for feat in load_builtin_attributes():
                config.attributes.register(feat)
        if config.intersection_attributes is None:
            config.intersection_attributes = default_intersection_attributes()
        return config


# ── Stage config slicing (relocated from pipeline.py, Phase 29a) ─────────────
# Pure BNAConfig serialization. ``_STAGE_CONFIG_FIELDS`` is the single source of
# truth (design-review D0 / Phase 30b) for BOTH what each stage *consumes* and what
# enters its *hash*: the engine builds the effective config, ``effective_stage_params``
# slices it per stage via ``config_slice_for_stage``, and that slice is fed to the
# planner/engine hash path. The access-audit test (tests/unit/test_30b.py) fails if a
# stage reads a config attribute not declared here. Each entry lists the top-level
# ``BNAConfig`` attributes (sub-config namespaces or global scalars) the stage reads.

def _serialize_config(obj: Any) -> Any:
    """Serialize a config object for hashing; handles RuleSets and registries."""
    from bikescore.decision import Decision
    if isinstance(obj, Decision):
        return obj.to_yaml()
    from bikescore.destinations import DestinationRegistry
    if isinstance(obj, DestinationRegistry):
        # DestinationRegistry is not a dataclass; use its stable to_dict() method
        # so the hash is content-based, not memory-address-based.
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _serialize_config(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, (list, tuple)):
        return [_serialize_config(x) for x in obj]
    return obj


# Top-level BNAConfig attributes each stage reads (namespaces + global scalars).
# Keep in sync with actual reads — the access-audit test enforces it.
_STAGE_CONFIG_FIELDS: dict[str, list[str]] = {
    "parse":        ["destinations", "attributes", "intersection_attributes"],
    "clip":         ["max_trip_distance"],
    "attributes":   ["city", "imputation", "attributes", "variables"],
    "segment":      ["output_srid", "destinations"],
    "stress":       ["stress", "variables"],
    "census":       ["block_boundary_overlap", "exclude_water_blocks"],
    "graph":        ["block_road_buffer", "block_road_min_length", "graph", "stress"],
    "connectivity": ["connectivity", "max_trip_distance"],
    "destinations": ["destinations", "max_trip_distance"],
    "jobs":         [],
    "scores":       ["destinations", "max_trip_distance", "min_bbox_length",
                     "min_path_length", "scoring"],
    "neighborhood": ["destinations"],
    "export":       ["export"],
}


def config_slice_for_stage(config: BNAConfig, stage: str) -> dict:
    """Return the serialized config fields that affect this stage."""
    fields = _STAGE_CONFIG_FIELDS.get(stage, [])
    result: dict[str, Any] = {}
    for f in fields:
        if f == "attributes":
            if config.attributes is None:
                continue
            serialized = config.attributes.to_dict()
            if serialized:
                result["attributes"] = serialized
        else:
            val = getattr(config, f, None)
            if f == "variables" and not val:
                continue  # empty variables: no hash contribution
            if val is not None:
                result[f] = _serialize_config(val)
    return result
