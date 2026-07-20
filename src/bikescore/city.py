"""City identity — static regional metadata stored in city.toml."""

from __future__ import annotations

import os
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


def save_city(city_dir: Path, city: CityIdentity) -> None:
    """Write CityIdentity to city.toml atomically."""
    city_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f'name = "{city.name}"\n',
        f'slug = "{city.slug}"\n',
        f'region = "{city.region}"\n',
        f'country = "{city.country}"\n',
    ]
    if city.fips_code is not None:
        lines.append(f'fips_code = "{city.fips_code}"\n')
    if city.timezone is not None:
        lines.append(f'timezone = "{city.timezone}"\n')
    toml_path = city_dir / "city.toml"
    tmp = toml_path.with_suffix(".toml.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, toml_path)
