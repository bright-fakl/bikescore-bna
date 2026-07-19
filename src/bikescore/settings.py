"""Global settings — ~/.config/bikescore/settings.toml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SETTINGS_PATH = Path.home() / ".config" / "bikescore" / "settings.toml"

# _SETTINGS_PATH and _BNA_ROOT are the patchable names used internally —
# tests monkeypatch these to redirect reads/writes away from the real file.
_SETTINGS_PATH = SETTINGS_PATH
_BNA_ROOT = SETTINGS_PATH.parent

_DEFAULT_DATA = Path.home() / ".local" / "share" / "bikescore"


@dataclass
class GlobalSettings:
    schema_version: int = 1
    project_root: Path = field(default_factory=lambda: _DEFAULT_DATA / "projects")
    pbf_cache_dir: Path = field(default_factory=lambda: _DEFAULT_DATA / "pbf")


def load_settings() -> GlobalSettings:
    """Load settings.toml. Returns defaults if file absent or malformed."""
    if not _SETTINGS_PATH.exists():
        return GlobalSettings()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(_SETTINGS_PATH, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return GlobalSettings()
    defaults = GlobalSettings()
    return GlobalSettings(
        schema_version=int(data.get("schema_version", 1)),
        project_root=Path(data.get("project_root", defaults.project_root)).expanduser(),
        pbf_cache_dir=Path(data.get("pbf_cache_dir", defaults.pbf_cache_dir)).expanduser(),
    )


def save_settings(s: GlobalSettings) -> None:
    """Write settings atomically to _SETTINGS_PATH."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"schema_version = {s.schema_version}\n",
        f'project_root = "{s.project_root}"\n',
        f'pbf_cache_dir = "{s.pbf_cache_dir}"\n',
    ]
    tmp = _SETTINGS_PATH.with_suffix(".toml.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(_SETTINGS_PATH)


def get_city_dir(slug: str) -> Path:
    """Return project_root / slug. Raises FileNotFoundError if not found."""
    city_dir = load_settings().project_root / slug
    if not city_dir.exists():
        raise FileNotFoundError(f"City directory not found: {city_dir}")
    return city_dir


# Backward-compat helpers used by CLI and service layer.
def default_project_root() -> Path:
    return load_settings().project_root


def default_pbf_cache_dir() -> Path:
    return load_settings().pbf_cache_dir
