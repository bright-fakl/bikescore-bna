# Retrospectives

The durable lessons from the `bna-core → bikescore-bna` carve-out, distilled from the
Phase 38 A-series. The full per-sub-phase retrospectives live in the frozen `bna-core`
repo (`phases/38*.md`); the ones that shape how the core is built are:

## The split

- **Identity, not "within tolerance."** The crux gate (38f) asserts the DB-free
  `score_city` output is *identical* to the oracle — zero differing rows and zero rows
  needing a known-deviation to match — not merely inside a deviation budget. Anything
  downstream of the `attributes` stage must reproduce the oracle value-for-value; a diff
  is a port bug to fix or escalate, never to paper over.
- **The stage seam is one primitive.** `run_stage` ("assemble input paths, call the
  stage, track the output dir") is shared by `score_city` and the app engine, so the two
  execution paths cannot drift. The app wraps it with hashing / reuse / persistence; the
  core loops it plainly.
- **Metadata that looks dead in core isn't.** A stage's `version` and config-slice are
  never read by `score_city`, but they are the app's cache-buster and invalidation key.
  They stay co-located with the stage; a core access-audit test keeps them honest so they
  aren't "cleaned up" by someone who only sees the core.

## Determinism is the reuse contract

Content-addressed reuse silently assumes each stage output is a deterministic function of
`(config-slice, upstream hashes, dataset hashes, version)`. No unseeded RNG, wall-clock
timestamps, or unordered output — a violation is a *silent* parity bug, not a crash. When
in doubt (e.g. wrapping a nondeterministic foreign library), set `cacheable=False`:
correctness over reuse.

## Ports leave clutter behind

The core is a clean-slate port, not a copy: dead code, stale scenarios, and accumulated
cruft were left in `bna-core` by construction. The check on each stage was the oracle
parquet, so a faithful port could be *smaller* than its source without losing behaviour.

## Docs and tooling

- **ruff is the gate; pyright is advisory.** CI fails on ruff findings; type errors are
  informational. Keep new code ruff-clean before committing.
- **The oracle baseline is gitignored** — it is split-time scaffolding regenerated
  locally, so parity tests *skip* (not fail) on a cold clone without it. The durable
  parity mechanism carried forward is the brokenspoke-analyzer reference-parquet
  comparison.
