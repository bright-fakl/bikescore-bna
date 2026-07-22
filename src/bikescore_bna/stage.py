"""The stage contract: ``StageSpec`` + the shared ``run_stage`` primitive.

This is the seam between the scoring *core* and any orchestration on top of it. A
stage is a plain data record (``StageSpec``) plus a run callable; there is no base
class, no ABC, and no import-side-effect registry. ``score_city`` (core) loops
``run_stage``; the app engine (Phase 38i) wraps the *same* primitive with hashing,
reuse, and persistence — one implementation of "assemble inputs, call the stage,
track the output dir", so the two paths cannot drift.

The metadata a stage declares (``depends_on`` / ``dataset_inputs`` / ``version`` and
the co-located :func:`bikescore_bna.config.config_slice_for_stage` /
``_STAGE_CONFIG_FIELDS``) is the **orchestration contract**. ``score_city`` reads only
``depends_on`` / ``dataset_inputs``; ``version`` and the config-slice look "dead" in
core, but they are the cache-buster and the fine-grained-invalidation key the app
relies on. They stay co-located with the stage that owns them (the author is the only
party who knows the right value); the core access-audit test keeps them honest. Do not
relocate them to the app — see index §7.3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bikescore_bna.config import BNAConfig

# A stage's run callable: ``run(input_paths, output_dir, config) -> None``.
# ``input_paths`` maps each upstream stage name -> that stage's output directory, and
# ``"dataset:<name>"`` -> the raw dataset file for each declared dataset input.
StageRun = Callable[[dict[str, Path], Path, "BNAConfig"], None]


@dataclass(frozen=True)
class StageSpec:
    """A single pipeline stage as a static, side-effect-free data record.

    **Determinism invariant (the reuse contract, index §7.3 / §8-B).** A stage's
    output MUST be a deterministic function of ``(config-slice, upstream output
    hashes, dataset hashes, version)`` and nothing else — no unseeded RNG, wall-clock
    timestamps, environment reads, or unordered output. Content-addressed reuse in the
    app silently assumes this: if two runs share those four inputs, the app reuses a
    cached output instead of recomputing. A stage that violates the invariant produces
    a silent parity bug, not a crash. If a stage cannot honour it (e.g. it wraps a
    nondeterministic foreign library), set ``cacheable=False`` so it always recomputes.

    Attributes:
        name: Unique stage name; the key under which the output dir is tracked and the
            directory basename ``score_city`` writes into.
        depends_on: Names of prior stages whose output dirs feed this one. Static,
            resolved *before* execution (no data-dependent graph shape). The app
            re-derives its DAG from this metadata — one source of truth for deps.
        dataset_inputs: Named external inputs the caller/orchestrator supplies (e.g.
            ``"osm"``, ``"boundary"``). Opaque to the orchestrator; *something* (an
            ``InputProvider``) must produce them.
        version: Author-declared cache-buster, bumped on behavioural change. Opaque to
            the orchestrator; enters the stage hash so a bump invalidates cached output.
        run: The compute callable ``run(input_paths, output_dir, config) -> None``.
        cacheable: When ``False`` the stage always recomputes (foreign/nondeterministic
            computation); the run still proceeds and downstream stays content-addressed
            on this stage's *actual* output hash. Safe default for anything uncertain is
            ``False`` — correctness over reuse (index §8.1).
    """

    name: str
    depends_on: tuple[str, ...]
    dataset_inputs: tuple[str, ...]
    version: str
    run: StageRun
    cacheable: bool = True


def run_stage(
    stage: StageSpec,
    upstream_dirs: dict[str, Path],
    dataset_paths: dict[str, Path],
    output_dir: Path,
    config: BNAConfig,
) -> None:
    """Assemble ``input_paths`` and invoke ``stage.run`` (the shared primitive).

    Builds the ``input_paths`` dict the stage expects — each ``depends_on`` name mapped
    to its upstream output directory, and each ``dataset_inputs`` name mapped under a
    ``"dataset:<name>"`` key to its resolved file path — then calls ``stage.run``.
    Dataset-path *resolution* deliberately stays out of this primitive: core resolves
    caller-supplied paths, the app resolves ``file_id -> path`` via its dataset store.
    Both hand the resolved ``dataset_paths`` in here.

    Args:
        stage: The stage to run.
        upstream_dirs: Maps upstream stage name -> its output directory. Must contain
            every name in ``stage.depends_on``.
        dataset_paths: Maps dataset-input name -> resolved file path. Must contain every
            name in ``stage.dataset_inputs``.
        output_dir: Directory the stage writes all its outputs into (already created).
        config: The effective ``BNAConfig`` for this run.

    Raises:
        KeyError: A declared ``depends_on`` / ``dataset_inputs`` name is missing from
            ``upstream_dirs`` / ``dataset_paths``.
    """
    input_paths: dict[str, Path] = {}
    for dep in stage.depends_on:
        input_paths[dep] = upstream_dirs[dep]
    for source_name in stage.dataset_inputs:
        input_paths[f"dataset:{source_name}"] = dataset_paths[source_name]
    stage.run(input_paths, output_dir, config)


# ── Attribute helpers (ported from the old ``BaseStage`` as free functions) ──────────
# Stages call these directly; there is no base class to inherit them. The registry
# objects (``AttributeRegistry``) carry the real logic — these thin wrappers add the
# ``None``-guard and the keyword-arg convention the stage compute functions use.


def apply_attributes(df: Any, attributes: Any, vars: dict | None = None) -> Any:
    """Apply all registered attributes' *observed* computes in topo order (Phase 34i).

    A ``None`` registry is a no-op (returns ``df`` unchanged).
    """
    if attributes is None:
        return df
    return attributes.apply_all(df, vars=vars or {})


def apply_attribute_fallbacks(
    df: Any,
    attributes: Any,
    vars: dict | None = None,
) -> Any:
    """Apply attribute fallback passes + auto ``{col}_imputed`` (Phase 35f).

    Must run *after* the observed attributes and class adjustments so FC-default
    fallbacks key on the promoted ``functional_class``. A ``None`` registry is a no-op.
    """
    if attributes is None:
        return df
    return attributes.apply_all_fallbacks(df, vars=vars or {})


def build_rule_variables(rule_variables: dict[str, str], config: BNAConfig) -> dict[str, Any]:
    """Resolve a stage's ``{var_name: dotted.config.path}`` map against *config*.

    Returns a dict mapping each variable name to its current config value (or ``None``
    if any path segment is missing/``None`` — the rule engine treats a ``None`` variable
    as "skip the corresponding rule").
    """
    variables: dict[str, Any] = {}
    for var_name, config_path in rule_variables.items():
        obj: Any = config
        for part in config_path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        variables[var_name] = obj
    return variables
