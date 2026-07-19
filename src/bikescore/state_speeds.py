"""US-state default-speed table — locale facts seeded into a city's overrides at init.

`city_default_speed` / `state_default_speed` answer "what speed do we assume on an
untagged road *here*" (design-review §2.4). They are locale facts that depend on a
state's speed laws, not reusable policy, so they live in the per-city override layer
(`bikescore.yaml: overrides.imputation`) rather than in a global scenario.

`init` seeds them from this table keyed off the city's `region` (state name) — then
leaves them editable. Values are mph. The table is intentionally small; unknown
states fall back to `_DEFAULT`.
"""

from __future__ import annotations

# (city_default_speed, state_default_speed) in mph, keyed by normalized state name.
_DEFAULT: tuple[int, int] = (25, 30)

_STATE_SPEEDS: dict[str, tuple[int, int]] = {
    "alabama": (25, 30),
    "alaska": (25, 30),
    "arizona": (25, 25),
    "arkansas": (30, 30),
    "california": (25, 25),
    "colorado": (25, 30),
    "connecticut": (25, 25),
    "delaware": (25, 25),
    "district of columbia": (25, 25),
    "florida": (30, 30),
    "georgia": (30, 30),
    "hawaii": (25, 25),
    "idaho": (25, 35),
    "illinois": (30, 30),
    "indiana": (30, 30),
    "iowa": (25, 25),
    "kansas": (30, 30),
    "kentucky": (25, 35),
    "louisiana": (25, 25),
    "maine": (25, 25),
    "maryland": (25, 30),
    "massachusetts": (25, 30),
    "michigan": (25, 25),
    "minnesota": (30, 30),
    "mississippi": (25, 30),
    "missouri": (25, 25),
    "montana": (25, 35),
    "nebraska": (25, 25),
    "nevada": (25, 25),
    "new hampshire": (30, 30),
    "new jersey": (25, 25),
    "new mexico": (30, 30),
    "new york": (25, 30),
    "north carolina": (35, 35),
    "north dakota": (25, 25),
    "ohio": (25, 25),
    "oklahoma": (25, 25),
    "oregon": (25, 25),
    "pennsylvania": (25, 35),
    "rhode island": (25, 25),
    "south carolina": (30, 30),
    "south dakota": (25, 25),
    "tennessee": (30, 30),
    "texas": (30, 30),
    "utah": (25, 25),
    "vermont": (25, 25),
    "virginia": (25, 25),
    "washington": (25, 25),
    "west virginia": (25, 25),
    "wisconsin": (25, 25),
    "wyoming": (30, 30),
}


def _normalize(region: str | None) -> str:
    return (region or "").strip().lower().replace("-", " ")


def default_speeds_for_region(region: str | None) -> tuple[int, int]:
    """Return (city_default_speed, state_default_speed) in mph for a state/region.

    Matching is case- and separator-insensitive ("New-Mexico" == "new mexico").
    Unknown regions return the conservative `_DEFAULT` (25, 30).
    """
    return _STATE_SPEEDS.get(_normalize(region), _DEFAULT)
