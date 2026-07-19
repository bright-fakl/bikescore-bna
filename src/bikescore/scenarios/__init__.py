"""Scenario library — a city resolves from one self-contained scenario (Phase 35j).

A *complete* scenario is a full, self-contained snapshot: all config options + all rule
sets + attributes + destinations explicit. It is the structural source of truth — the
resolver block-replaces from it honouring deletion (supplement M1). A *sparse* scenario
is a YAML dict of config-namespace deltas read over package defaults (for scalar-only or
minimal scenarios). There is **no scenario stack** (M2): customise by *deriving* a new
scenario from a base (copy + edit), recording a lightweight ``base:`` reference.

Bundled scenarios live under ``bikescore/scenarios/data/``. User scenarios live at
``project_root/scenarios/{name}.yaml``. The bundled ``default`` scenario is complete and
is what new cities are seeded from.

Type discriminator in YAML:
  ``type: complete`` — self-contained structural source (block-replaced on load)
  ``type: sparse``   — config delta over package defaults
  absent             — treated as ``sparse``
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

GLOBAL_DEFAULT_SCENARIO = "default"


class ScenarioNotFoundError(Exception):
    """Raised when a requested scenario (or version) does not exist."""


def _bundled_scenarios_dir():
    return files("bikescore.scenarios") / "data"


def _user_scenarios_dir(project_root: Path) -> Path:
    return Path(project_root) / "scenarios"


def global_default_scenario() -> str | None:
    """Return the name of the global default scenario, if it is bundled."""
    try:
        if (_bundled_scenarios_dir() / f"{GLOBAL_DEFAULT_SCENARIO}.yaml").is_file():
            return GLOBAL_DEFAULT_SCENARIO
    except (FileNotFoundError, OSError):
        pass
    return None


def list_catalogs_detailed(
    project_root: Path, city_dir: Path | None = None
) -> list[dict]:
    """Return catalog metadata for each available catalog.

    Returns list of {name, source, type_count} dicts.
    source is one of: "city", "project", "bundled".
    """
    import yaml as _yaml

    from bikescore.destinations import _catalog_search_paths

    search_paths = _catalog_search_paths(project_root, city_dir)
    if city_dir is not None:
        source_labels = ["city", "project", "bundled"]
    else:
        source_labels = ["project", "bundled"]

    seen: set[str] = set()
    result: list[dict] = []
    for directory, source in zip(search_paths, source_labels):
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.yaml")):
            name = p.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                type_count = len(raw.get("destinations", []))
            except Exception:
                type_count = 0
            result.append({"name": name, "source": source, "type_count": type_count})
    return result


def list_catalogs(project_root: Path, city_dir: Path | None = None) -> list[str]:
    """Return de-duped catalog names from all resolution paths (no .yaml extension).

    Order: city-scoped > project-scoped > bundled. Same name from multiple paths
    appears once (first-found wins).
    """
    from bikescore.destinations import _catalog_search_paths

    seen: set[str] = set()
    result: list[str] = []
    for directory in _catalog_search_paths(project_root, city_dir):
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.yaml")):
            name = p.stem
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _scenario_type(content: dict) -> str:
    """Return the scenario type string; absent type: field → 'sparse' (backward compat)."""
    return content.get("type", "sparse")


def list_scenarios(project_root: Path) -> list[dict]:
    """Return [{name, source, description, type, default}] for bundled and user scenarios."""
    results: list[dict] = []

    bundled = _bundled_scenarios_dir()
    seen_names: set[str] = set()
    for f in sorted(bundled.iterdir()):
        if not f.name.endswith(".yaml"):
            continue
        stem = f.name.removesuffix(".yaml")
        # Skip versioned files (name@N) — only the unversioned alias appears in list
        if "@" in stem:
            continue
        content = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        results.append({
            "name": stem,
            "source": "builtin",
            "description": content.get("description", ""),
            "type": _scenario_type(content),
            "default": stem == GLOBAL_DEFAULT_SCENARIO,
        })
        seen_names.add(stem)

    user_dir = _user_scenarios_dir(project_root)
    if user_dir.exists():
        for f in sorted(user_dir.glob("*.yaml")):
            content = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            results.append({
                "name": f.stem,
                "source": "user",
                "description": content.get("description", ""),
                "type": _scenario_type(content),
                "default": False,
            })

    return results


def get_scenario(project_root: Path, name: str, version: int | None = None) -> str | None:
    """Return raw YAML text for a named scenario.

    User scenarios take precedence over bundled ones (no version support for user).
    When ``version`` is given, loads the versioned bundled file ``{name}@{version}.yaml``.
    Raises ScenarioNotFoundError if a versioned file is requested but not found.
    """
    if version is None:
        user_path = _user_scenarios_dir(project_root) / f"{name}.yaml"
        if user_path.exists():
            return user_path.read_text(encoding="utf-8")

        bundled_path = _bundled_scenarios_dir() / f"{name}.yaml"
        try:
            text = bundled_path.read_text(encoding="utf-8")
            return text
        except FileNotFoundError:
            return None
    else:
        # Versioned lookup — bundled only
        versioned_path = _bundled_scenarios_dir() / f"{name}@{version}.yaml"
        try:
            return versioned_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ScenarioNotFoundError(
                f"Bundled scenario {name!r} version {version} not found."
            )


def load_scenario_dict(project_root: Path, name: str) -> dict[str, Any]:
    """Return the parsed config dict for a scenario (``description`` and ``type`` dropped).

    Raises ValueError if the scenario does not exist.
    """
    text = get_scenario(project_root, name)
    if text is None:
        raise ValueError(f"Scenario {name!r} not found")
    content: dict[str, Any] = yaml.safe_load(text) or {}
    content.pop("description", None)
    content.pop("type", None)
    content.pop("version", None)
    return content


# ── Ruleset <-> scenario-namespace map (mirrors config_resolver._RULESET_LOCATIONS) ──

#: ruleset name (CLI/web vocabulary) -> (config namespace, field) in a scenario dict.
_RULESET_LOCATIONS: dict[str, tuple[str, str]] = {
    "stress_segment": ("stress", "segment_rules"),
    "stress_intersection": ("stress", "intersection_rules"),
}


def get_scenario_ruleset(
    project_root: Path, name: str, ruleset_name: str
) -> Any | None:
    """Return the decision value stored for one ruleset in a scenario, or None.

    Reads the named scenario (user or bundled) and returns the raw decision value
    found at its ``[namespace][field]`` location — the canonical ``{name, passes}``
    dict the rule-builder writes, or a hand-authored terse form. Returns None when
    the scenario does not define the ruleset (or the scenario is missing). Raises
    ValueError for an unknown ruleset name.
    """
    if ruleset_name not in _RULESET_LOCATIONS:
        raise ValueError(f"Unknown ruleset {ruleset_name!r}")
    ns, field = _RULESET_LOCATIONS[ruleset_name]
    text = get_scenario(project_root, name)
    if text is None:
        return None
    content = yaml.safe_load(text) or {}
    if not isinstance(content, dict):
        return None
    return (content.get(ns) or {}).get(field)


def list_bundled_scenarios() -> list[str]:
    """Return the names of bundled scenarios (unversioned ``name`` aliases only).

    Read-only discovery for the core library — no workspace / user scenarios (those
    are an orchestration concern). Versioned files (``name@N.yaml``) are excluded.
    """
    names: list[str] = []
    for f in sorted(_bundled_scenarios_dir().iterdir()):
        if not f.name.endswith(".yaml"):
            continue
        stem = f.name.removesuffix(".yaml")
        if "@" in stem:
            continue
        names.append(stem)
    return names
