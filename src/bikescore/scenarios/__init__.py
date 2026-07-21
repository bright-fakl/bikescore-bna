"""Scenario library — a city resolves from one self-contained scenario (Phase 35j).

A *complete* scenario is a full, self-contained snapshot: all config options + all rule
sets + attributes + destinations explicit. It is the structural source of truth — the
resolver block-replaces from it honouring deletion (supplement M1). A *sparse* scenario
is a YAML dict of config-namespace deltas read over package defaults (for scalar-only or
minimal scenarios). There is **no scenario stack** (M2): customise by *deriving* a new
scenario from a base (copy + edit), recording a lightweight ``base:`` reference.

Bundled scenarios ship with the package under ``bikescore/scenarios/data/``. The bundled
``default`` scenario is complete and is the starting point users copy and edit
(``scenario show`` dumps its YAML). User/workspace scenario *libraries* — saving, versioning,
deriving — are an orchestration-layer concern, not part of the scoring core.

Type discriminator in YAML:
  ``type: complete`` — self-contained structural source (block-replaced on load)
  ``type: sparse``   — config delta over package defaults
  absent             — treated as ``sparse``
"""

from __future__ import annotations

from importlib.resources import files


class ScenarioNotFoundError(Exception):
    """Raised when a requested scenario (or version) does not exist."""


def _bundled_scenarios_dir():
    return files("bikescore.scenarios") / "data"


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


def get_bundled_scenario(name: str) -> str:
    """Return the raw YAML text of the bundled scenario ``name`` (e.g. ``"default"``).

    ``name`` may pin a version (``"default@1"``). Raises :class:`ScenarioNotFoundError`
    if no such bundled scenario exists. This is the *obtain* half of the customise loop:
    ``scenario show`` (or this call) → edit the YAML → ``build_config(Path(...))`` /
    ``score --scenario file.yaml``.
    """
    path = _bundled_scenarios_dir() / f"{name}.yaml"
    if not path.is_file():
        raise ScenarioNotFoundError(f"No bundled scenario named {name!r}")
    return path.read_text()
