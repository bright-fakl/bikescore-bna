"""US state/city default-speed resolution from the brokenspoke-analyzer ground-truth tables.

Residential roads with no OSM ``maxspeed`` fall back to a city default, then a state
default (design-review §2.4; brokenspoke ``COALESCE(speed_limit, :city_default,
:state_default)``). Those two scalars are *locale facts* keyed by the city's FIPS place
code, resolved here from the national tables core ships in ``bikescore/data/``:

* ``state_fips_speed.csv`` — ``state, fips_code_state, speed`` (keyed by 2-digit state FIPS)
* ``city_fips_speed.csv``  — ``city, state, fips_code_city, speed`` (keyed by 7-digit place FIPS)

These are the same tables brokenspoke imports (``speed_tables.sql``) — the single source of
truth. ``resolve_city_speed_defaults`` fills ``config.city.default_speed`` /
``state_default_speed`` from a city's FIPS unless already set; an explicit config value
(scenario / ``--set`` / user edit) always wins, matching brokenspoke's
``city_speed_limit_override``. Cities absent from ``city_fips_speed.csv`` resolve to
``None`` for the city default (COALESCE then falls through to the state default, then the
per-functional-class fallback). Non-US cities have no FIPS and resolve to ``None``.

This is the single home of speed-default data + lookup; the app derives nothing itself,
it only hands a ``CityIdentity`` to :func:`resolve_city_speed_defaults`.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bikescore.city import CityIdentity
    from bikescore.config import BNAConfig


def _load(filename: str, key_col: str, key_width: int) -> dict[str, int]:
    text = (files("bikescore.data") / filename).read_text(encoding="utf-8")
    out: dict[str, int] = {}
    for row in csv.DictReader(text.splitlines()):
        key = (row.get(key_col) or "").strip()
        speed = (row.get("speed") or "").strip()
        if key and speed:
            out[key.zfill(key_width)] = int(speed)
    return out


@lru_cache(maxsize=1)
def _state_speeds() -> dict[str, int]:
    """Map 2-digit state FIPS -> default residential speed (mph)."""
    return _load("state_fips_speed.csv", "fips_code_state", 2)


@lru_cache(maxsize=1)
def _city_speeds() -> dict[str, int]:
    """Map 7-digit place FIPS -> default residential speed (mph)."""
    return _load("city_fips_speed.csv", "fips_code_city", 7)


def state_default_speed(fips_code: str | None) -> int | None:
    """State default residential speed (mph) for a city's FIPS place code, or None."""
    if not fips_code:
        return None
    return _state_speeds().get(fips_code.strip().zfill(7)[:2])


def city_default_speed(fips_code: str | None) -> int | None:
    """City default residential speed (mph) for a city's FIPS place code, or None."""
    if not fips_code:
        return None
    return _city_speeds().get(fips_code.strip().zfill(7))


def resolve_city_speed_defaults(config: BNAConfig, identity: CityIdentity) -> None:
    """Fill ``config.city`` speed defaults from the city's FIPS, unless already set.

    Mutates ``config`` in place. An explicit ``default_speed`` / ``state_default_speed``
    (from the scenario, ``--set``, or a user edit) is left untouched — it always wins,
    matching brokenspoke's ``city_speed_limit_override``. US cities only; non-US
    identities have no FIPS and resolve to the per-functional-class fallback.
    """
    fips = identity.fips_code
    if config.city.default_speed is None:
        config.city.default_speed = city_default_speed(fips)
    if config.city.state_default_speed is None:
        config.city.state_default_speed = state_default_speed(fips)
