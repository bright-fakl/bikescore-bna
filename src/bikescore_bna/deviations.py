"""Known intentional deviations from the brokenspoke-analyzer SQL reference.

Two kinds of deviations are tracked:

KnownDeviation — column-value differences on matched rows
    An SQL bug bna-core deliberately fixes.  The match() callable identifies
    which (col, computed, reference, row) tuples are explained.
    Adding a new column deviation:
    1. Implement the fix in the relevant stage.
    2. Add a KnownDeviation entry to KNOWN_DEVIATIONS.
    3. match() receives: col, computed, reference, computed_row (full row dict).

KnownAbsenceDeviation — rows present in reference but absent from computed
    An architectural difference (not a bug) causing bna-core to omit rows that
    the SQL reference keeps as an artifact of its pipeline ordering.
    Identification is done at the validation layer (pipeline.py), which has
    access to the stage-specific context needed to determine which reference-only
    rows are explained.
    Adding a new absence deviation:
    1. Document here.
    2. Update pipeline.validate() to set report.rows_only_reference_expected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnownDeviation:
    """A documented SQL bug that bna-core intentionally fixes."""

    name: str
    """Stable kebab-case identifier, e.g. 'park-both-overwrite'."""

    stage: str
    """Pipeline stage whose output is affected, e.g. 'attributes'.
    Matched as a prefix so 'classify [isolated: parse]' also matches."""

    columns: list[str]
    """Output columns where this deviation manifests."""

    description: str
    """Human-readable explanation of the SQL bug and how bna-core fixes it."""

    sql_ref: str
    """Source SQL file and relevant UPDATE number, e.g. 'features/park.sql UPDATE 2-3'."""

    match: Callable[[str, Any, Any, dict[str, Any]], bool]
    """Return True when (col, computed, reference, computed_row) is explained by this deviation."""

    shadow_columns: dict[str, str] = field(default_factory=dict)
    """Output column → shadow column name (e.g. {'tf_bike_infra': 'tf_bike_infra_sql'}).

    When present, validation checks shadow_col == reference_value for exact
    shadow-confirmed identification, falling back to match() otherwise.
    Shadow columns are only present in computed_full when
    config.classification.compute_shadows is True."""


@dataclass
class KnownAbsenceDeviation:
    """A documented architectural difference causing rows to be absent from computed.

    Unlike KnownDeviation (which fixes SQL bugs), absence deviations reflect cases
    where bna-core's behaviour is *more correct* than the SQL reference, but the
    difference is an artifact of pipeline ordering rather than a computable fix.

    Identification of which reference-only rows belong to this deviation is done
    externally in pipeline.validate() — it requires stage-specific context (e.g.
    the set of segment orphan osm_ids) that is not available inside compare_dataframes.
    """

    name: str
    """Stable kebab-case identifier."""

    stage: str
    """Pipeline stage where the absence manifests."""

    description: str
    """Human-readable explanation of why rows are absent from computed."""

    sql_ref: str
    """Source SQL file or architectural note."""


# ── Column-value deviation registry ───────────────────────────────────────────

def _park_both_match(col: str, cv: Any, rv: Any, row: dict[str, Any]) -> bool:
    """Match rows where the park :both-overwrite bug explains the difference."""
    if col not in ("ft_park", "tf_park"):
        return False
    # bna-core correctly preserves a :both result; SQL's right/left passes then
    # overwrite it unconditionally, destroying the :both value.
    # Indicator: computed has a value, reference disagrees, and the row has a
    # :both tag set (cycleway:both or parking:lane:both).
    if cv is None:
        return False
    both_tags = ("cycleway:both", "parking:lane:both")
    return any(
        str(row.get(t) or "").strip() not in ("", "no", "none")
        for t in both_tags
    )


def _bike_infra_tf_track_match(col: str, cv: Any, rv: Any, row: dict[str, Any]) -> bool:
    """Match rows where the tf opposite-track dead-code bug explains the difference."""
    if col != "tf_bike_infra":
        return False
    # SQL dead-code bug: inside WHEN one_way_car='ft' THEN CASE, two conditions
    # check one_way_car='tf' (always false there), making opposite-track assignment
    # for tf on ft one-way roads unreachable. bna-core assigns 'track' correctly.
    return (
        cv == "track"
        and rv is None
        and str(row.get("one_way_car") or "") == "ft"
    )


KNOWN_DEVIATIONS: list[KnownDeviation] = [
    KnownDeviation(
        name="park-both-overwrite",
        stage="attributes",
        columns=["ft_park", "tf_park"],
        description=(
            "SQL park.sql runs three sequential UPDATEs (both, right, left). "
            "The right and left passes overwrite the :both result unconditionally, "
            "so a road with only cycleway:both gets its ft_park or tf_park cleared "
            "if no matching :right or :left tag is present. "
            "bna-core evaluates the three passes independently and preserves the "
            ":both result when :right/:left would produce NULL."
        ),
        sql_ref="features/park.sql UPDATE 2-3",
        match=_park_both_match,
        shadow_columns={"ft_park": "ft_park_sql", "tf_park": "tf_park_sql"},
    ),
    KnownDeviation(
        name="bike-infra-tf-track-deadcode",
        stage="attributes",
        columns=["tf_bike_infra"],
        description=(
            "SQL bike_infra.sql copy-paste bug: inside the WHEN one_way_car='ft' "
            "outer CASE branch, two inner conditions check one_way_car='tf' (always "
            "false in that branch), making opposite-track assignment for tf on "
            "ft one-way roads unreachable. bna-core adds the missing 'track' "
            "assignment for those two conditions."
        ),
        sql_ref="features/bike_infra.sql WHEN one_way_car='ft' inner CASE",
        match=_bike_infra_tf_track_match,
        shadow_columns={"tf_bike_infra": "tf_bike_infra_sql"},
    ),
]


# ── Row-absence deviation registry ────────────────────────────────────────────

KNOWN_ABSENCE_DEVIATIONS: list[KnownAbsenceDeviation] = [
    KnownAbsenceDeviation(
        name="topology-ordering-orphan",
        stage="attributes",
        description=(
            "brokenspoke-analyzer builds its road topology (via osm2pgrouting) "
            "BEFORE functional_class.sql deletes NULL-FC ways. Ways that share a "
            "node only with a deleted way get split at that node first, producing "
            "a two-segment chain whose segments reference each other — allowing "
            "both to pass the SQL one-hop orphan check even though the whole "
            "cluster is disconnected from the main road network.\n"
            "bna-core runs classify (which deletes NULL-FC ways) BEFORE segment "
            "(which builds topology). Without the deleted connecting way the "
            "remaining ways are correctly identified as orphans and dropped.\n"
            "bna-core's behaviour is more correct: these isolated dead-end clusters "
            "are unreachable from any census block and contribute nothing to BNA "
            "scores. The reference keeps them as an artifact of topology-first "
            "ordering."
        ),
        sql_ref="features/functional_class.sql orphan DELETE (one-hop limitation)",
    ),
]
