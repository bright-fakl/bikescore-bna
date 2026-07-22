"""FIPS-based state/city default-speed resolution over the shipped ground-truth tables."""

from __future__ import annotations

from bikescore.city import CityIdentity
from bikescore.config import BNAConfig
from bikescore.state_speeds import (
    city_default_speed,
    resolve_city_speed_defaults,
    state_default_speed,
)


def _identity(fips: str | None) -> CityIdentity:
    return CityIdentity(name="X", slug="x", region="R", country="united states", fips_code=fips)


def test_lookup_known_city_and_state() -> None:
    # Aspen, CO (place FIPS 0803620) — the validation city.
    assert city_default_speed("0803620") == 20
    assert state_default_speed("0803620") == 30  # Colorado state default


def test_city_absent_returns_none_state_still_resolves() -> None:
    # A CO place FIPS not in city_fips_speed.csv → city None, state from the 2-digit prefix.
    assert city_default_speed("0899999") is None
    assert state_default_speed("0899999") == 30


def test_none_and_nonus_fips() -> None:
    assert city_default_speed(None) is None
    assert state_default_speed(None) is None


def test_resolve_fills_unset_only() -> None:
    config = BNAConfig.with_defaults()
    assert config.city.default_speed is None
    resolve_city_speed_defaults(config, _identity("0803620"))
    assert config.city.default_speed == 20
    assert config.city.state_default_speed == 30


def test_resolve_respects_explicit_override() -> None:
    config = BNAConfig.with_defaults()
    config.city.default_speed = 15  # explicit (scenario / --set / user edit)
    resolve_city_speed_defaults(config, _identity("0803620"))
    assert config.city.default_speed == 15  # override wins
    assert config.city.state_default_speed == 30  # unset one still filled
