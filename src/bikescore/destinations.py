"""Destination type extensibility — OSM matchers, clustering, and scoring params.

Destination matching uses the canonical decision DSL's row-level
:class:`~bikescore.decision.Matcher` (an ``any``-of-rows boolean predicate, the one
place OR lives — design-review A.6.6), replacing the former bespoke ``OsmMatcher``
grammar. The first row of a node matcher is *primary* (the SQL precedence quirk
where its points are always included, even inside polygon clusters).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bikescore.decision import Clause, Matcher, MatchRow

# ── Matcher construction helpers ──────────────────────────────────────────────

def _clause_from_cond(key: str, expected: Any) -> Clause:
    """Compile one terse OSM tag condition into a :class:`Clause`.

    ``None`` ⇒ tag present (any value); ``"!"`` ⇒ tag absent; list ⇒ value in list;
    scalar ⇒ exact match.
    """
    if expected == "!":
        return Clause(key, "absent")
    if expected is None:
        return Clause(key, "present")
    if isinstance(expected, list):
        return Clause(key, "in", expected)
    return Clause(key, "eq", expected)


def _row(conditions: dict[str, Any]) -> MatchRow:
    """A single AND row of tag conditions."""
    return MatchRow(tuple(_clause_from_cond(k, v) for k, v in conditions.items()))


def matcher(*rows: dict[str, Any]) -> Matcher:
    """Build an ``any``-of-rows :class:`Matcher` from terse condition dicts."""
    return Matcher(tuple(_row(r) for r in rows))


# ── Compact catalog helpers ───────────────────────────────────────────────────

_CLUSTERING_MAP: dict[str, tuple[str, int]] = {
    "individual": ("no_cluster", 0),
    "poly": ("poly_cluster", 50),
    "retail": ("retail", 50),
    "transit": ("transit", 75),
}


def _catalog_search_paths(project_root: Path, city_dir: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if city_dir is not None:
        paths.append(Path(city_dir) / "destinations")
    paths.append(Path(project_root) / "destinations")
    paths.append(Path(__file__).parent / "data" / "destinations")
    return paths


def _catalog_type_from_compact(d: dict) -> DestinationType:
    """Build a DestinationType from compact catalog YAML."""
    name = d["name"]
    display_name = d.get("display_name", name)

    clustering_raw = d.get("clustering", "poly")
    if clustering_raw not in _CLUSTERING_MAP:
        raise ValueError(f"Unknown clustering shorthand: {clustering_raw!r}")
    clustering_mode, default_tolerance = _CLUSTERING_MAP[clustering_raw]
    clustering_tolerance_m = d.get("clustering_tolerance_m", default_tolerance)

    scoring_raw = list(d.get("scoring") or [])
    scoring = DestinationScoringParams(
        first=scoring_raw[0] if len(scoring_raw) > 0 else 0.0,
        second=scoring_raw[1] if len(scoring_raw) > 1 else 0.0,
        third=scoring_raw[2] if len(scoring_raw) > 2 else 0.0,
        max_score=1.0,
    )

    match_raw = d.get("match") or {}
    node_match = Matcher(tuple(_row(r) for r in (match_raw.get("nodes") or [])))
    area_match = Matcher(tuple(_row(r) for r in (match_raw.get("areas") or [])))
    exclude_match = Matcher(tuple(_row(r) for r in (match_raw.get("exclude") or [])))
    way_match = Matcher(tuple(_row(r) for r in (match_raw.get("ways") or [])))

    return DestinationType(
        name=name,
        display_name=display_name,
        node_match=node_match,
        area_match=area_match,
        exclude_match=exclude_match,
        clustering_tolerance_m=clustering_tolerance_m,
        clustering_mode=clustering_mode,
        scoring_category=d.get("category", "core_services"),
        category_weight=d.get("weight"),
        scoring=scoring,
        type=d.get("type", "poi"),
        way_match=way_match,
        human_explanation=d.get("human_explanation", ""),
        score_noun=d.get("score_noun", ""),
        score_noun_singular=d.get("score_noun_singular", ""),
        score_noun_access=d.get("score_noun_access", ""),
        enabled=d.get("enabled", True),
        score_id=d.get("score_id", ""),
    )



@dataclass
class DestinationScoringParams:
    """Piecewise scoring thresholds for counting reachable destination clusters.

    Matches the first/second/third parameters in access_*.sql.

    first=0.7  → reaching 1 cluster gives score 0.7
    second=0.2 → reaching 2 clusters adds 0.2 (total 0.9)
    third=0.0  → third threshold not used (score continues linearly to max_score)
    """

    first: float = 0.0
    second: float = 0.0
    third: float = 0.0
    max_score: float = 1.0


@dataclass
class DestinationType:
    """Full specification of a destination type.

    Covers OSM matching, spatial clustering, scoring category membership,
    and scoring parameters.

    The 13 standard BNA types are defined in default_destination_registry().
    Users add custom types via DestinationRegistry.register().
    """

    name: str
    """Unique identifier. Used as column prefix in output (e.g. 'schools_score')."""

    display_name: str
    """Human-readable label for reports and documentation."""

    node_match: Matcher = field(default_factory=Matcher)
    """Match OSM node (point) features — an ``any``-of-rows matcher.
    The first row is primary (always included even inside polygon clusters)."""

    area_match: Matcher = field(default_factory=Matcher)
    """Match OSM closed-way (polygon) features — an ``any``-of-rows matcher."""

    exclude_match: Matcher = field(default_factory=Matcher)
    """Exclude features matching any of its rows — applied after node/area matching."""

    type: str = "poi"
    """Pseudo-type discriminator. ``"poi"`` (default) is a point/area destination
    matched and clustered by the destinations stage. ``"network_path"`` is a linear
    network feature (trails) detected by the segment stage from ``way_match`` and
    scored via reverse-Dijkstra in the scores stage — it is *not* a POI type and is
    skipped by the POI-extraction/scan/report paths (parse, destinations, neighborhood)."""

    way_match: Matcher = field(default_factory=Matcher)
    """Match network ways (segments) — an ``any``-of-rows matcher. Used only by
    ``network_path`` entries; empty for POI types."""

    clustering_tolerance_m: int = 50
    """DBSCAN eps in projected CRS metres. 0 = no clustering (each POI is its own cluster)."""

    clustering_mode: str = "poly_cluster"
    """Spatial clustering algorithm used by the destinations stage.

    ``"poly_cluster"`` (default): cluster polygons with DBSCAN into merged MultiPolygons;
        add points that fall outside all cluster polygons as standalone records.
    ``"no_cluster"``: each polygon is its own record; remove sub-polygons (those
        contained within a larger polygon); add standalone points outside polygons.
    ``"retail"``: combine all polygons and 10m-buffered points, then cluster the
        combined set; each cluster becomes one MultiPolygon record.
    ``"transit"``: keep each polygon as an individual record (remove sub-polygons);
        cluster points with DBSCAN, excluding points within clustering_tolerance_m of any
        polygon.
    """

    scoring_category: str = "core_services"
    """BNA scoring category — any non-empty label.

    The standard BNA categories are 'opportunity', 'core_services', 'recreation',
    'retail', 'transit', but a catalog may declare new categories (Phase 34e).
    Every active category must have a matching ``scoring.category_weights`` entry;
    this is enforced by ``validate_scoring_categories`` at config-validation time.
    """

    category_weight: float | None = None
    """Weight of this type within its category. None = auto-normalize.

    Explicit weights across all types in a category must sum to <= 1.0.
    Unweighted types share the remaining weight equally.
    """

    scoring: DestinationScoringParams = field(default_factory=DestinationScoringParams)

    human_explanation: str = ""
    """Destination-specific wording for the score_inputs human_explanation column.

    Must match the SQL reference exactly for the 13 standard types
    (e.g. "grocery stores" not "supermarkets", "doctors office" not "doctor").
    Populated for standard types in default_destination_registry().
    Custom destination types may use any descriptive text.
    Falls back to display_name if empty.
    """

    # ── score_inputs text-template fields ────────────────────────────────────

    score_noun: str = ""
    """Plural noun used throughout score_inputs row score_names and descriptions.
    Falls back to display_name in the template.
    Examples: 'schools', 'transit', 'retail', 'tech/vocational colleges'."""

    score_noun_singular: str = ""
    """Singular form for r+1-3 score_names ('Median score of {X} access')
    and r+5-8 score_names ('Average {X} bike shed access score',
    'Median {X} population shed score', etc.).
    Falls back to score_noun. Set only when singular differs: 'school', 'university',
    'tech/vocational college'."""

    score_noun_access: str = ""
    """Noun for the r+4 score_name ('Average score of access to {X}').
    Falls back to human_explanation. Set only when the r+4 score_name noun
    differs from human_explanation: hospitals='hospitals', supermarkets='grocery stores'."""

    enabled: bool = True
    """Disabled types are not scanned, not scored, not reported."""

    score_id: str = ""
    """Row identifier emitted in the neighborhood overall_scores table.

    Empty string (default) derives as '{scoring_category}_{name}'.
    Set explicitly for standard BNA types to match the SQL reference
    (e.g. 'schools' → 'opportunity_k12_education',
    'supermarkets' → 'core_services_grocery', 'retail' → 'retail').
    """

    def __post_init__(self) -> None:
        # Categories are open (Phase 34e): any non-empty label is allowed. An
        # unknown category is caught downstream by validate_scoring_categories
        # against scoring.category_weights — a clearer error than a fixed
        # whitelist, and the single source of truth for valid categories.
        if not self.scoring_category:
            raise ValueError("scoring_category must be a non-empty string")
        if not self.human_explanation:
            self.human_explanation = self.display_name

    def to_dict(self) -> dict:
        """Stable, deterministic serialization (matchers as ``any``-of-rows dicts)."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "node_match": self.node_match.to_dict(),
            "area_match": self.area_match.to_dict(),
            "exclude_match": self.exclude_match.to_dict(),
            "type": self.type,
            "way_match": self.way_match.to_dict(),
            "clustering_tolerance_m": self.clustering_tolerance_m,
            "clustering_mode": self.clustering_mode,
            "scoring_category": self.scoring_category,
            "category_weight": self.category_weight,
            "scoring": {
                "first": self.scoring.first,
                "second": self.scoring.second,
                "third": self.scoring.third,
                "max_score": self.scoring.max_score,
            },
            "human_explanation": self.human_explanation,
            "score_noun": self.score_noun,
            "score_noun_singular": self.score_noun_singular,
            "score_noun_access": self.score_noun_access,
            "enabled": self.enabled,
            "score_id": self.score_id,
        }




class DestinationRegistry:
    """Manages the set of active destination types.

    Default instance (from default_destination_registry()) contains the 13 standard
    BNA types with parameters that reproduce the SQL reference on Washington DC.
    """

    def __init__(self, types: list[DestinationType] | None = None) -> None:
        self._types: dict[str, DestinationType] = {}
        self._source_name: str | None = None
        for t in (types or []):
            self._types[t.name] = t

    def register(self, dest: DestinationType) -> None:
        """Add a new destination type.

        Raises:
            ValueError: if name conflicts with an existing type.
        """
        if dest.name in self._types:
            raise ValueError(
                f"Destination type '{dest.name}' already exists. "
                f"Use registry.replace() to update it."
            )
        self._types[dest.name] = dest

    def get(self, name: str) -> DestinationType:
        """Return a destination type by name. Raises KeyError if not found."""
        if name not in self._types:
            raise KeyError(f"Destination type '{name}' not found")
        return self._types[name]

    def replace(self, name: str, dest: DestinationType) -> None:
        """Replace an existing destination type (e.g. to modify OSM matchers)."""
        if name not in self._types:
            raise KeyError(f"Destination type '{name}' not found")
        self._types[name] = dest

    def disable(self, name: str) -> None:
        """Disable a destination type. Re-enable with enable()."""
        if name not in self._types:
            raise KeyError(f"Destination type '{name}' not found")
        object.__setattr__(self._types[name], "enabled", False)

    def enable(self, name: str) -> None:
        """Re-enable a disabled destination type."""
        if name not in self._types:
            raise KeyError(f"Destination type '{name}' not found")
        object.__setattr__(self._types[name], "enabled", True)

    def active(self) -> list[DestinationType]:
        """Return all enabled destination types in insertion order."""
        return [t for t in self._types.values() if t.enabled]

    def by_category(self, category: str) -> list[DestinationType]:
        """Return active destination types for a scoring category."""
        return [t for t in self.active() if t.scoring_category == category]

    def resolved_weights(self, category: str) -> dict[str, float]:
        """Return normalized category weights for active types in a category.

        Types with explicit category_weight use that value.
        Types with None share the remaining weight equally.
        Raises ValueError if explicit weights sum > 1.0.
        """
        members = self.by_category(category)
        if not members:
            return {}
        explicit = {t.name: t.category_weight for t in members if t.category_weight is not None}
        explicit_sum = sum(explicit.values())
        if explicit_sum > 1.0 + 1e-9:
            raise ValueError(
                f"Category '{category}' explicit weights sum to {explicit_sum:.3f} > 1.0"
            )
        unweighted = [t for t in members if t.category_weight is None]
        remaining = 1.0 - explicit_sum
        auto_weight = remaining / len(unweighted) if unweighted else 0.0
        return {
            t.name: (explicit[t.name] if t.name in explicit else auto_weight)
            for t in members
        }

    def validate(self) -> None:
        """Validate all category weight sums. Called on pipeline startup."""
        # Categories are whatever the active types declare (Phase 34e), not a
        # fixed whitelist.
        for category in {t.scoring_category for t in self.active()}:
            self.resolved_weights(category)  # raises on invalid weights

    def to_dict(self) -> dict[str, dict]:
        """Stable, deterministic serialization for cache hashing.

        Returns a dict keyed by destination name (insertion order) containing
        each type's ``to_dict()``. Used by _serialize_config so the destinations
        config slice produces the same hash across processes.
        """
        return {name: dt.to_dict() for name, dt in self._types.items()}

    def to_yaml(self) -> str:
        """Serialize the whole registry to a YAML string (atomic, whole-block).

        Round-trips with ``DestinationRegistry.from_yaml`` / ``from_dict``. Used to
        express destinations in scenario / city YAML, where the entire registry is
        one atomic config entry (D2 / design-review §2.1).
        """
        import yaml

        return yaml.dump(
            {"destinations": list(self.to_dict().values())},
            sort_keys=False,
            allow_unicode=True,
        )

    @classmethod
    def from_dict(cls, data: dict | list) -> DestinationRegistry:
        """Reconstruct a registry from the structure produced by ``to_dict``/``to_yaml``.

        Accepts either ``{"destinations": [...]}``, a bare list of type dicts, or a
        dict keyed by destination name. The whole block replaces the registry.
        """
        if isinstance(data, dict):
            raw = data.get("destinations", data)
        else:
            raw = data
        if isinstance(raw, dict):
            items = list(raw.values())
        else:
            items = list(raw or [])
        return cls([_destination_type_from_dict(d) for d in items])

    @classmethod
    def from_yaml(cls, source: str | Path) -> DestinationRegistry:
        """Load a registry from a YAML string or file path produced by ``to_yaml``."""
        import yaml

        text = source
        if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source
                                        and Path(source).exists()):
            text = Path(source).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text) or {})

    @classmethod
    def from_catalog(
        cls, name: str, project_root: Path, city_dir: Path | None = None
    ) -> DestinationRegistry:
        """Load a named catalog YAML file. Sets ``_source_name``."""
        search = _catalog_search_paths(project_root, city_dir)
        for directory in search:
            candidate = directory / f"{name}.yaml"
            if candidate.exists():
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                items = raw.get("destinations", [])
                types = [_catalog_type_from_compact(d) for d in items]
                reg = cls(types)
                reg._source_name = name
                return reg
        valid = [
            p.stem
            for d in search
            if d.exists()
            for p in sorted(d.glob("*.yaml"))
        ]
        raise ValueError(
            f"Destination catalog {name!r} not found. "
            f"Searched: {[str(p) for p in search]}. "
            f"Available: {sorted(set(valid))}"
        )

    def __len__(self) -> int:
        return len(self._types)


def _destination_type_from_dict(d: dict) -> DestinationType:
    """Reconstruct a DestinationType from its ``to_dict`` form."""
    scoring = d.get("scoring") or {}
    return DestinationType(
        name=d["name"],
        display_name=d.get("display_name", d["name"]),
        node_match=Matcher.from_dict(d.get("node_match")),
        area_match=Matcher.from_dict(d.get("area_match")),
        exclude_match=Matcher.from_dict(d.get("exclude_match")),
        type=d.get("type", "poi"),
        way_match=Matcher.from_dict(d.get("way_match")),
        clustering_tolerance_m=d.get("clustering_tolerance_m", 50),
        clustering_mode=d.get("clustering_mode", "poly_cluster"),
        scoring_category=d.get("scoring_category", "core_services"),
        category_weight=d.get("category_weight"),
        scoring=DestinationScoringParams(**scoring),
        human_explanation=d.get("human_explanation", ""),
        score_noun=d.get("score_noun", ""),
        score_noun_singular=d.get("score_noun_singular", ""),
        score_noun_access=d.get("score_noun_access", ""),
        enabled=d.get("enabled", True),
        score_id=d.get("score_id", ""),
    )


def default_destination_registry() -> DestinationRegistry:
    """Return the 13 standard BNA destination types.

    Parameters reproduce the SQL reference output exactly on Washington DC.
    Category weights match category_scores.sql:
      opportunity:   emp=0.35(*), k12/schools=0.35, colleges=0.10, universities=0.20
      core_services: doctors=0.20, dentists=0.10, hospitals=0.20,
                     pharmacies=0.10, supermarkets=0.25, social_services=0.15
      recreation:    parks=0.40, community_centers=0.25, trails=0.35 (network_path)
      retail:        retail=1.00
      transit:       transit=1.00

    (*) employment (jobs) is not a destination type — it's loaded from LODES data.
    schools weight here covers k12 education.
    """
    types = [
        # ── Opportunity ────────────────────────────────────────────────────────
        DestinationType(
            name="schools",
            display_name="K-12 Schools",
            node_match=matcher({"amenity": ["school", "kindergarten"]}),
            area_match=matcher({"amenity": ["school", "kindergarten"]}),
            clustering_tolerance_m=0,
            clustering_mode="no_cluster",
            scoring_category="opportunity",
            category_weight=0.35,
            scoring=DestinationScoringParams(first=0.3, second=0.2, third=0.2),
            human_explanation="K12 schools",
            score_noun="schools",
            score_noun_singular="school",
            score_id="opportunity_k12_education",
        ),
        DestinationType(
            name="colleges",
            display_name="Technical/Vocational Colleges",
            node_match=matcher({"amenity": "college"}),
            area_match=matcher({"amenity": "college"}),
            clustering_tolerance_m=100,
            clustering_mode="poly_cluster",
            scoring_category="opportunity",
            category_weight=0.10,
            scoring=DestinationScoringParams(first=0.7),
            human_explanation="tech/vocational colleges",
            score_noun="tech/vocational colleges",
            score_noun_singular="tech/vocational college",
            score_id="opportunity_technical_vocational_college",
        ),
        DestinationType(
            name="universities",
            display_name="Universities",
            node_match=matcher({"amenity": "university"}),
            area_match=matcher({"amenity": "university"}),
            clustering_tolerance_m=150,
            clustering_mode="poly_cluster",
            scoring_category="opportunity",
            category_weight=0.20,
            scoring=DestinationScoringParams(first=0.7),
            human_explanation="universities",
            score_noun="universities",
            score_noun_singular="university",
            score_id="opportunity_higher_education",
        ),
        # ── Core services ──────────────────────────────────────────────────────
        DestinationType(
            name="doctors",
            display_name="Doctors / Clinics",
            node_match=matcher(
                {"amenity": ["clinic", "doctors"]},
                {"healthcare": ["doctor", "doctors", "clinic"]},
            ),
            area_match=matcher(
                {"amenity": ["clinic", "doctors"]},
                {"healthcare": ["doctor", "doctors", "clinic"]},
            ),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="core_services",
            category_weight=0.20,
            scoring=DestinationScoringParams(first=0.4, second=0.2, third=0.1),
            human_explanation="doctors",
            score_noun="doctors",
            score_id="core_services_doctors",
        ),
        DestinationType(
            name="dentists",
            display_name="Dentists",
            node_match=matcher({"amenity": "dentist"}, {"healthcare": "dentist"}),
            area_match=matcher({"amenity": "dentist"}, {"healthcare": "dentist"}),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="core_services",
            category_weight=0.10,
            scoring=DestinationScoringParams(first=0.4, second=0.2, third=0.1),
            human_explanation="dentists",
            score_noun="dentists",
            score_id="core_services_dentists",
        ),
        DestinationType(
            name="hospitals",
            display_name="Hospitals",
            node_match=matcher(
                {"amenity": ["hospital", "hospitals"]},
                {"healthcare": "hospital"},
            ),
            area_match=matcher(
                {"amenity": ["hospital", "hospitals"]},
                {"healthcare": "hospital"},
            ),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="core_services",
            category_weight=0.20,
            scoring=DestinationScoringParams(first=0.7),
            human_explanation="hospital",
            score_noun="hospitals",
            score_noun_access="hospitals",
            score_id="core_services_hospitals",
        ),
        DestinationType(
            name="pharmacies",
            display_name="Pharmacies",
            node_match=matcher({"amenity": "pharmacy"}, {"shop": "chemist"}),
            area_match=matcher({"amenity": "pharmacy"}, {"shop": "chemist"}),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="core_services",
            category_weight=0.10,
            scoring=DestinationScoringParams(first=0.4, second=0.2, third=0.1),
            human_explanation="pharmacies",
            score_noun="pharmacies",
            score_id="core_services_pharmacies",
        ),
        DestinationType(
            name="supermarkets",
            display_name="Grocery Stores",
            node_match=matcher({"shop": "supermarket"}),
            area_match=matcher({"shop": "supermarket"}),
            clustering_tolerance_m=0,
            clustering_mode="no_cluster",
            scoring_category="core_services",
            category_weight=0.25,
            scoring=DestinationScoringParams(first=0.6, second=0.2),
            human_explanation="grocery",
            score_noun="supermarkets",
            score_noun_access="grocery stores",
            score_id="core_services_grocery",
        ),
        DestinationType(
            name="social_services",
            display_name="Social Services",
            node_match=matcher({"amenity": "social_facility"}),
            area_match=matcher({"amenity": "social_facility"}),
            clustering_tolerance_m=0,
            clustering_mode="no_cluster",
            scoring_category="core_services",
            category_weight=0.15,
            scoring=DestinationScoringParams(first=0.7),
            human_explanation="social services",
            score_noun="social services",
            score_id="core_services_social_services",
        ),
        # ── Recreation ─────────────────────────────────────────────────────────
        DestinationType(
            name="parks",
            display_name="Parks",
            node_match=matcher(
                {"amenity": "park"},
                {"leisure": ["park", "nature_reserve", "playground"]},
            ),
            area_match=matcher(
                {"amenity": "park"},
                {"leisure": ["park", "nature_reserve", "playground"]},
            ),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="recreation",
            category_weight=0.40,
            scoring=DestinationScoringParams(first=0.3, second=0.2, third=0.2),
            human_explanation="parks",
            score_noun="parks",
            score_id="recreation_parks",
        ),
        DestinationType(
            name="community_centers",
            display_name="Community Centers",
            node_match=matcher({"amenity": ["community_centre", "community_center"]}),
            area_match=matcher({"amenity": ["community_centre", "community_center"]}),
            clustering_tolerance_m=50,
            clustering_mode="poly_cluster",
            scoring_category="recreation",
            category_weight=0.25,
            scoring=DestinationScoringParams(first=0.4, second=0.2, third=0.1),
            human_explanation="community centers",
            score_noun="community centers",
            score_id="recreation_community_centers",
        ),
        # Trails — a `network_path` pseudo-destination (Phase 34f). Detected by the
        # segment stage from `way_match` (functional_class == "path", reproducing the
        # legacy hard-coded filter) and scored via reverse Dijkstra in the scores stage.
        # First-class recreation member with an explicit, validated weight: 0.35
        # completes parks(0.40) + community_centers(0.25) = 1.00 (the legacy trail slot).
        DestinationType(
            name="trails",
            display_name="Trails",
            type="network_path",
            way_match=matcher({"functional_class": "path"}),
            scoring_category="recreation",
            category_weight=0.35,
            scoring=DestinationScoringParams(first=0.7, second=0.2),
            human_explanation="trails",
            score_noun="trails",
            score_id="recreation_trails",
        ),

        # ── Retail ─────────────────────────────────────────────────────────────
        DestinationType(
            name="retail",
            display_name="Retail",
            node_match=matcher({"shop": None}),  # any shop tag
            area_match=matcher(
                {"landuse": "retail"},
                {"building": "retail"},
                {"shop": None},
            ),
            exclude_match=matcher(
                {"shop": "supermarket"},  # supermarkets scored separately
                {"shop": "no"},
            ),
            clustering_tolerance_m=50,
            clustering_mode="retail",
            scoring_category="retail",
            category_weight=1.00,
            scoring=DestinationScoringParams(first=0.4, second=0.2, third=0.1),
            human_explanation="retail",
            score_noun="retail",
            score_id="retail",
        ),
        # ── Transit ────────────────────────────────────────────────────────────
        DestinationType(
            name="transit",
            display_name="Transit Stops",
            node_match=matcher(
                {"amenity": ["bus_station", "ferry_terminal"]},
                {"railway": "station"},
                {"public_transport": "station"},
            ),
            area_match=matcher(
                {"amenity": ["bus_station", "ferry_terminal"]},
                {"railway": "station"},
            ),
            exclude_match=matcher(
                {"aerialway": None},   # exclude ski gondola stations
            ),
            clustering_tolerance_m=75,
            clustering_mode="transit",
            scoring_category="transit",
            category_weight=1.00,
            scoring=DestinationScoringParams(first=0.6),
            human_explanation="transit",
            score_noun="transit",
            score_id="transit",
        ),
    ]
    return DestinationRegistry(types)


# ── Phase 35a — catalog authoring (editor round-trip + file CRUD) ─────────────

_CLUSTERING_MODE_TO_SHORTHAND: dict[str, str] = {
    "no_cluster": "individual",
    "poly_cluster": "poly",
    "retail": "retail",
    "transit": "transit",
}


def _clause_to_terse_value(c: Clause) -> Any:
    """Reverse of ``_clause_from_cond``: a Clause → a terse catalog value.

    ``present`` ⇒ ``None``; ``absent`` ⇒ ``"!"``; ``in`` ⇒ list; ``eq`` ⇒ scalar.
    Any other op cannot be expressed in the compact terse grammar.
    """
    if c.op == "present":
        return None
    if c.op == "absent":
        return "!"
    if c.op == "in":
        return list(c.value)
    if c.op == "eq":
        return c.value
    raise ValueError(
        f"Matcher clause {c.field} {c.op!r} cannot be expressed in a compact catalog"
    )


def _matcher_to_terse(m: Matcher) -> list[dict]:
    """Serialize a Matcher to compact terse rows (``[{tag: value}, ...]``)."""
    return [
        {clause.field: _clause_to_terse_value(clause) for clause in row.clauses}
        for row in m.rows
    ]


def _editor_dict_from_type(dt: DestinationType) -> dict:
    """Stable, all-keys-present editor shape (terse matchers) for the catalog editor."""
    return {
        "name": dt.name,
        "display_name": dt.display_name,
        "score_id": dt.score_id,
        "category": dt.scoring_category,
        "weight": dt.category_weight,
        "clustering": _CLUSTERING_MODE_TO_SHORTHAND.get(dt.clustering_mode, "poly"),
        "clustering_tolerance_m": dt.clustering_tolerance_m,
        "type": dt.type,
        "enabled": dt.enabled,
        "scoring": [dt.scoring.first, dt.scoring.second, dt.scoring.third],
        "match": {
            "nodes": _matcher_to_terse(dt.node_match),
            "areas": _matcher_to_terse(dt.area_match),
            "exclude": _matcher_to_terse(dt.exclude_match),
            "ways": _matcher_to_terse(dt.way_match),
        },
        "human_explanation": dt.human_explanation,
        "score_noun": dt.score_noun,
        "score_noun_singular": dt.score_noun_singular,
        "score_noun_access": dt.score_noun_access,
    }


def _compact_dict_from_type(dt: DestinationType) -> dict:
    """Minimal, canonical compact YAML dict for one type (matches bundled catalog form)."""
    out: dict[str, Any] = {"name": dt.name}
    if dt.display_name and dt.display_name != dt.name:
        out["display_name"] = dt.display_name
    if dt.score_id:
        out["score_id"] = dt.score_id
    out["category"] = dt.scoring_category
    if dt.category_weight is not None:
        out["weight"] = dt.category_weight
    out["clustering"] = _CLUSTERING_MODE_TO_SHORTHAND.get(dt.clustering_mode, "poly")
    out["clustering_tolerance_m"] = dt.clustering_tolerance_m
    if dt.type != "poi":
        out["type"] = dt.type
    if not dt.enabled:
        out["enabled"] = False
    out["scoring"] = [dt.scoring.first, dt.scoring.second, dt.scoring.third]
    match: dict[str, list] = {}
    if dt.node_match.rows:
        match["nodes"] = _matcher_to_terse(dt.node_match)
    if dt.area_match.rows:
        match["areas"] = _matcher_to_terse(dt.area_match)
    if dt.exclude_match.rows:
        match["exclude"] = _matcher_to_terse(dt.exclude_match)
    if dt.way_match.rows:
        match["ways"] = _matcher_to_terse(dt.way_match)
    if match:
        out["match"] = match
    if dt.human_explanation and dt.human_explanation != dt.display_name:
        out["human_explanation"] = dt.human_explanation
    for key, val in (
        ("score_noun", dt.score_noun),
        ("score_noun_singular", dt.score_noun_singular),
        ("score_noun_access", dt.score_noun_access),
    ):
        if val:
            out[key] = val
    return out


_CATALOG_SOURCES = ("city", "project", "bundled")


def catalog_source(
    project_root: Path, name: str, city_dir: Path | None = None
) -> str | None:
    """Return the highest-priority source a catalog ``name`` resolves to, or None.

    Mirrors ``_catalog_search_paths`` ordering: city > project > bundled.
    """
    search = _catalog_search_paths(project_root, city_dir)
    labels = _CATALOG_SOURCES if city_dir is not None else _CATALOG_SOURCES[1:]
    for directory, source in zip(search, labels):
        if (directory / f"{name}.yaml").exists():
            return source
    return None


def _editable_catalog_path(
    project_root: Path, name: str, city_dir: Path | None
) -> Path:
    """Filesystem path for an editable (project- or city-level) catalog."""
    base = Path(city_dir) if city_dir is not None else Path(project_root)
    return base / "destinations" / f"{name}.yaml"


def load_catalog_for_edit(
    project_root: Path, name: str, city_dir: Path | None = None
) -> dict:
    """Return ``{name, source, editable, description, types: [editor-dict]}``.

    Raises ValueError if the catalog does not exist at any resolution level.
    """
    source = catalog_source(project_root, name, city_dir)
    if source is None:
        raise ValueError(f"Destination catalog {name!r} not found")
    reg = DestinationRegistry.from_catalog(name, project_root, city_dir)
    # The file may carry a free-form description alongside its destinations.
    description = ""
    for directory in _catalog_search_paths(project_root, city_dir):
        candidate = directory / f"{name}.yaml"
        if candidate.exists():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            description = raw.get("description", "") if isinstance(raw, dict) else ""
            break
    return {
        "name": name,
        "source": source,
        "editable": source != "bundled",
        "description": description,
        "types": [_editor_dict_from_type(t) for t in reg._types.values()],
    }


def _build_types_or_raise(types: list[dict]) -> list[DestinationType]:
    """Reconstruct DestinationTypes from editor/compact dicts, validating each.

    Raises ValueError on a malformed type or matcher, or on a duplicate name.
    """
    built: list[DestinationType] = []
    seen: set[str] = set()
    for i, d in enumerate(types):
        if not isinstance(d, dict):
            raise ValueError(f"Type #{i} must be a mapping")
        try:
            dt = _catalog_type_from_compact(d)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid destination type #{i}: {exc}")
        if dt.name in seen:
            raise ValueError(f"Duplicate destination type name {dt.name!r}")
        seen.add(dt.name)
        built.append(dt)
    return built


def save_catalog(
    project_root: Path,
    name: str,
    types: list[dict],
    *,
    city_dir: Path | None = None,
    description: str = "",
    create: bool = False,
) -> None:
    """Validate ``types`` and write a project- or city-level catalog YAML.

    ``create=True`` is a POST (must not already exist at the editable level and must
    not shadow a bundled name silently — see callers); ``create=False`` is a PUT
    replace of an existing editable catalog. Bundled-source guarding is the caller's
    responsibility (it owns the 403). Raises ValueError on a bad name or a malformed
    type/matcher.
    """
    if not name or "/" in name or name.endswith(".yaml"):
        raise ValueError(f"Invalid catalog name {name!r}")

    built = _build_types_or_raise(types)
    out_path = _editable_catalog_path(project_root, name, city_dir)
    # A new catalog must have a name that does not already resolve at any level —
    # this also forbids shadowing a bundled catalog (bundled names are reserved).
    if create and catalog_source(project_root, name, city_dir) is not None:
        raise FileExistsError(f"Catalog {name!r} already exists")

    doc: dict[str, Any] = {"name": name}
    if description:
        doc["description"] = description
    doc["destinations"] = [_compact_dict_from_type(dt) for dt in built]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def delete_catalog(
    project_root: Path, name: str, city_dir: Path | None = None
) -> None:
    """Delete a project- or city-level catalog file. Raises ValueError if absent."""
    out_path = _editable_catalog_path(project_root, name, city_dir)
    if not out_path.exists():
        raise ValueError(f"Catalog {name!r} not found at an editable level")
    out_path.unlink()


def scenarios_referencing_catalog(project_root: Path, name: str) -> list[str]:
    """Names of scenarios whose ``destinations`` field selects catalog ``name``."""
    from bikescore.scenarios import get_scenario, list_scenarios

    referencing: list[str] = []
    for s in list_scenarios(project_root):
        text = get_scenario(project_root, s["name"])
        if text is None:
            continue
        content = yaml.safe_load(text) or {}
        if not isinstance(content, dict):
            continue
        section = content.get("config") if isinstance(content.get("config"), dict) else content
        if section.get("destinations") == name:
            referencing.append(s["name"])
    return referencing
