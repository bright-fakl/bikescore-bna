"""City identity — static regional metadata stored in city.toml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

_US_COUNTRY_NAMES = frozenset({"united states", "us", "usa", "united states of america"})


@dataclass
class CityIdentity:
    name: str
    slug: str
    region: str
    country: str
    fips_code: str | None = None
    timezone: str | None = None

    @property
    def is_us(self) -> bool:
        """True for US cities (census/LODES acquisition applies only to these)."""
        return self.country.lower() in _US_COUNTRY_NAMES


def load_city(city_dir: Path) -> CityIdentity:
    """Parse city.toml. Raises FileNotFoundError if missing."""
    toml_path = city_dir / "city.toml"
    if not toml_path.exists():
        raise FileNotFoundError(f"city.toml not found in {city_dir}")
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    city_data = data.get("city", data)
    return CityIdentity(
        name=city_data["name"],
        slug=city_data["slug"],
        region=city_data["region"],
        country=city_data["country"],
        fips_code=city_data.get("fips_code") or city_data.get("fips"),
        timezone=city_data.get("timezone"),
    )


