# Spec — Boundary manipulation, excluded-block layer, and multi-region acquisition

Status: **implemented** (Phase 1 + Phase 2) · Owner: Fabian · Last updated: 2026-07-25

## Motivation

Two observations drove this work:

1. **Excluded blocks look like missing data.** The census stage drops water-only
   blocks (`aland20 == 0`) and blocks less than `block_boundary_overlap` inside the
   boundary. On a map these appear as holes — sometimes populated enclaves (other
   municipalities) surrounded by scored blocks. They are *correctly* excluded from
   scoring, but there is no artifact that lets a viewer distinguish "water" /
   "out-of-city" from "missing". (See the Madison investigation: 592 of 4469 raw
   blocks dropped — 55 water-only, 537 out-of-boundary, ~1,095 residents in enclaves.)

2. **The analysis boundary is fixed to the fetched city polygon.** There is no way to
   fill enclave holes, restrict to a test sub-area, or otherwise reshape the region —
   all of which are useful for analysis and for fast test runs.

The design also has to respect a hard constraint the boundary work exposes: **any
transform that enlarges the analysis extent beyond the already-downloaded regional
data requires re-clipping — and possibly re-downloading — OSM, census, and LODES.**

## Decisions locked in

- **Excluded-block layer is always emitted** as a *separate* export layer with a class
  code. It is inert: the scoring pipeline never sees it, so scores and default parity
  are unchanged. No config flag — an inert artifact does not need to be opt-in.
- **`make_valid` is always-on** boundary hygiene.
- **Box-clip is intersected with the real boundary** (keeps realistic city edges).
- **A single `prepare_boundary(boundary, config)`** runs at **acquire time** and
  produces the **analysis boundary** consumed by both `parse` (OSM clip) and `census`
  (block filter). One source of truth; transforms propagate everywhere.
- **Both boundaries are persisted.** The **original** fetched city boundary is kept as
  a distinct artifact (provenance / identity); the derived **analysis** boundary is
  saved separately and is what stages consume. When no transform is configured the two
  are identical, so default output is byte-for-byte unchanged.
- **No "buffer the scoring boundary" transform.** A positive buffer erodes/fills
  interior holes by its radius (see [Buffering and holes](#buffering-and-holes)), which
  would silently annex enclaves. Growing the scored area is done by the exact transforms
  `fill_boundary_holes` / `convex_hull`, or by supplying an `override_geometry` **source**
  — never by buffering the scoring boundary.
- **Multi-region expansion is explicit first** (`extra_regions`), with an **advisory
  auto-detector** as a guard. Silent auto-fetch is deferred.

## Architecture

### Boundary source (fetch vs override)

**`override_geometry` is a source, not a transform.** The acquire chokepoint
(`UsCensusLodesProvider.acquire`, `acquire.py:466`) selects the source boundary:

```
source = override_geometry (user GeoJSON/bbox)  if set  else  _fetch_boundary_tmp(city)
```

The transform pipeline below then runs on top of whichever source was chosen. The
"original/provenance" boundary is the source.

### `prepare_boundary(source_gdf, config) -> GeoDataFrame`

A pure geometry transform applied once, at acquire time, to the source boundary, in
this order:

1. `make_valid` (always) — repair self-intersections before anything else.
2. `keep_largest_part` (optional) — drop detached exclaves/islands from a MultiPolygon.
3. `fill_boundary_holes` (optional) — rebuild each polygon from its exterior ring,
   absorbing interior enclaves.
4. `convex_hull` (optional) — replace with the convex hull.
5. Region restriction (optional): `clip_shape` (`box` | `circle`) sized by
   `clip_size_m` (box side length / circle diameter), centered on the boundary
   centroid, **∩ boundary**. One shape enum + one size avoids the
   two-mutually-exclusive-`_m`-fields footgun.

All metric operations run in the projected CRS (`output_srid`, else UTM estimate).

### Why acquire-time

`parse._run` and `census._run` both read the boundary; `parse()` then clips the PBF to
`boundary + buffer_m` via `pre_clip_pbf`. Transforming the boundary once, upstream,
means the OSM clip and the block filter stay consistent — a newly-included enclave gets
both its roads (in the clipped PBF) and its blocks (past the overlap test).

### Boundary provenance — original vs analysis

Two artifacts are persisted:

- **Original** — the boundary exactly as fetched (`_fetch_boundary_*`). Identity and
  provenance; never mutated. Exported as `neighborhood_boundary`.
- **Analysis** — `prepare_boundary(original, config)`. What `parse` and `census`
  consume, and the reference for the excluded-block `outside` test. Exported as
  `neighborhood_analysis_boundary` **only when it differs** from the original.

Wiring: acquire writes both (`dataset:boundary` = original for provenance,
`dataset:analysis_boundary` = derived); `parse`/`census`/`segment` are repointed to the
analysis artifact. With no transforms configured, `prepare_boundary` is identity, the
two artifacts are equal, the extra export layer is suppressed, and all stage outputs are
unchanged — preserving default parity.

### Buffering and holes

A positive buffer is morphological dilation: the exterior ring grows outward by `d`,
while every interior ring (hole/enclave) is offset *inward* — a hole shrinks by `d` all
around and vanishes if narrower than ~`2d`. The pipeline has **two independent buffers**
and treats holes differently by design:

- **Network buffer** (`pre_clip_pbf`, ≈ `max_trip_distance`) applies only to the
  **road-clip** region, never to the scoring boundary. Filling an enclave hole here is
  harmless and mildly correct: the enclave's roads become routable (a border block can
  cycle *through* it), but scores are unaffected because population/destinations come
  from census blocks inside the scoring boundary — and enclave blocks are not in the
  census output, so there is nothing extra to reach. No special handling; plain buffer.
- **Scoring boundary** — never buffered (see Decisions). Growing the scored area uses
  exact ops: `fill_boundary_holes` removes interior rings *completely* (predictable, not
  radius-dependent); `convex_hull` has no interior rings so it also drops all holes;
  `override_geometry` is explicit.

Because `fill_boundary_holes` runs in `prepare_boundary` (scoring boundary) and the
network buffer is applied later only to the road clip, the two never conflict: the scored
area is exactly what the transforms produce, with the network margin a separate wider
road-only halo on top.

### Expanding vs. subsetting transforms

The critical split — which transforms can push the analysis extent **beyond the
downloaded regional data**:

| Transform | Effect on extent | Needs re-acquire? |
|---|---|---|
| make_valid, keep_largest_part | shrink / same | no |
| clip (box/circle) | shrink | no |
| fill_boundary_holes | fills interior holes only | no — the enclave lies within the already-downloaded regional (state) PBF; re-clip the filled analysis boundary from it |
| convex_hull | can bulge outward | **maybe** |
| override_geometry (source) | arbitrary | **maybe** |
| non-zero network buffer near a region edge | outward | **maybe** |

"Maybe" = only when the transformed **+ network-buffer** extent exceeds the fetched
region(s). For an interior city it usually still fits the state extract; for DC any
outward expansion crosses into MD/VA and forces multi-region acquisition.

---

## Phase 1 — self-contained (no acquire/PBF changes)

Everything here stays within the current clipped PBF (`boundary + buffer`).

### 1. Excluded-block layer

- `census.filter_census_blocks` computes `block_class` for every block:
  `water` (`aland20 == 0`) → else `outside` (overlap `< block_boundary_overlap`) → else
  `included`.
- Returns **included-only** as today (byte-identical default; parity-safe) **and**
  writes a sibling `excluded_census_blocks.parquet` (geometry + `block_class` + key
  census attrs: `geoid20, pop20, aland20, awater20`).
- New `export.py` `ExportTarget` `neighborhood_excluded_blocks` (geo kind) reads that
  parquet; **skipped cleanly when the file is absent** (e.g. nothing excluded). Zero
  change to the existing `neighborhood_census_blocks` target.

### 2. `prepare_boundary` + `make_valid` + dual boundary artifacts

- Add the helper; `make_valid` always on.
- Acquire persists **both** boundaries: `dataset:boundary` (original, provenance) and
  `dataset:analysis_boundary` (`prepare_boundary` output). Repoint `parse`, `census`,
  and `segment` to consume the analysis artifact.
- Export: keep `neighborhood_boundary` (original); add `neighborhood_analysis_boundary`
  emitted **only when it differs** from the original (a cheap `geom_equals` /
  transform-configured check), so default bundles are unchanged.

### 3. Subsetting transforms

- `clip_shape` + `clip_size_m`, `keep_largest_part`, `fill_boundary_holes`.
- `fill_boundary_holes` lands here because the PBF clip now uses the (filled) **analysis**
  boundary and the enclave lies within the already-downloaded regional (state) PBF — so
  it is a re-clip from cached data, no new download. (The current exact-boundary clip,
  `buffer_m = 0`, would otherwise drop enclave roads that don't cross the outer ring.)

### 4. Tests

- `block_class` assignment (water / outside / included) on a crafted fixture.
- Excluded layer emitted with codes when present; export target skipped when absent.
- Each transform's geometry (hole filled, box/radius bounded and intersected, largest
  part kept).
- **Guard: default output byte-identical when no transform is set** — protects the
  existing scored `census_blocks.parquet` / `neighborhood_census_blocks`.

---

## Phase 2 — cross-region acquisition (expanding transforms)

Needed only when the transformed **+ network-buffer** extent exceeds the fetched
region(s). "Region" resolves per source; for US cities all three key the same **states**:

| Source | Region unit | How multi-region works |
|---|---|---|
| OSM (Geofabrik) | named extract (US state slug / country) | download each, **`osmium merge`** |
| Census (pygris) | US state FIPS | `pygris.blocks` per state, concat rows |
| LODES | US state abbr | per-state OD CSVs, concat rows |
| Non-US census/LODES | — | synthetic; covers any boundary, no expansion |

### 5. Explicit `extra_regions`

- Config list, e.g. `["district-of-columbia", "maryland", "virginia"]`.
- OSM: fetch each via the existing `_download_state_pbf`, then merge (below).
- Census/LODES: resolve each region to FIPS/abbr, fetch per state, concat. (No
  geometry to reconcile — plain GEOID-keyed row concat.)

### 6. OSM merge — seam stitching

Merging Geofabrik extracts is an **exact ID-based set union, not a fuzzy geometric
stitch**, because (a) OSM node/way IDs are globally unique and stable, and (b)
Geofabrik extracts are reference-complete (`complete_ways`): a border-crossing way is
present *in full*, with all its nodes, in every extract it touches. So the crossing way
deduplicates to a single connected way that ties the two networks together — no gaps,
no dangling stubs. Conditions that preserve this, all mandatory:

- **Use `osmium merge`, not `osmium cat`.** `merge` deduplicates by identity
  (type+id+version) and preserves sort order; `cat` concatenates and would leave
  duplicate IDs at every seam (risking doubled edges).
- **Download all regions in one batch** (shared snapshot date) to avoid version/
  timestamp skew, where the same border node appears at two versions.
- **Require the `osmium` CLI when `len(regions) > 1`** and error clearly if missing;
  there is no clean large-merge path in pyosmium.
- Our downstream `pre_clip_pbf` already clips with `complete_ways` (CLI) /
  `BackReferenceWriter` (pyosmium), so it will not re-truncate ways at the buffer edge.
- **Non-Geofabrik / bbox-truncated inputs void the guarantee** (ways cut at the edge,
  missing nodes) — document that `extra_regions` expects reference-complete extracts.

### 7. Expanding transforms

- `convex_hull`, `override_geometry` — depend on #5/#6 when they cross a region line.

### 8. Network buffer — currently disabled; enabling is opt-in and parity-affecting

Reality check: the mechanism exists (`pre_clip_pbf` + `_buffered_wgs84`) but the **only
call site passes `0.0`** (`acquire.py:493`), so the road network is clipped to the
**exact** boundary — the only spillover is `complete_ways` keeping border-*crossing*
ways whole. So there is currently **no buffer margin**, which is consistent with the
default (exact-clip) behaviour and is a plausible contributor to the edge-score
depression noted in the scores investigation.

- Thread `network_buffer_m` from config to the `pre_clip_pbf` call (replacing the
  hard-coded `0.0`), and clip the **analysis** boundary (not the original).
- **Default `0.0`** — a non-zero buffer changes the clipped PBF → the parsed network →
  scores, so it **deviates from default output** and must be opt-in.
- **A non-zero buffer expands the clip extent and therefore MUST go through the region
  coverage check** (§9). This applies to *any* buffer value, including whatever the
  existing mechanism would produce — the buffered extent, not the bare boundary, is what
  can spill past the downloaded region(s). Near a region edge (the DC case) even a modest
  buffer crosses into a neighboring state.
- Follow-up validation: confirm graph/connectivity actually traverse the buffered
  external roads (spot-check a border block) so the accuracy benefit is real.

**Interaction with the current clip algorithm.** The buffer is not a new clip step — it
enlarges the polygon *before* the existing algorithm runs, so it composes with
`complete_ways` rather than conflicting: a way is included if any node lands in the
dilated polygon, and all its nodes are kept. Effect is **monotonic** (strictly adds
roads) and the `complete_ways` fringe simply relocates beyond `boundary + buffer`. Three
consequences to handle:

- **Backend divergence — bug to fix.** `_clip_with_osmium_cli` does a true polygon clip;
  `_clip_with_pyosmium` does a **bbox** clip (pass 1 filters only on
  `boundary_geom.bounds`, no polygon test). Since `bbox ⊇ polygon` the pyosmium path
  over-includes corner roads and diverges from the CLI path — so the network depends on
  whether the `osmium` binary is present. Dormant when osmium CLI is used (the default in CI),
  but wrong otherwise, and a buffer widens the bbox and amplifies it. **Fix: give the
  pyosmium path a real polygon test** (not merely document it).
- **`complete_ways` over-reach — bounded downstream.** A long way with one node inside
  the polygon is kept in full in the *PBF/parse* output, so it still affects PBF/parse
  size. But it is a non-returning exit, so the segment split clips it at the boundary and
  it never enters the effective routing network (§8b). So this is at most a size
  consideration, not a routing/scoring one.
- **Border cap.** On a single state extract, `complete_ways` yields only the ways that
  *straddle* the region line, not the neighbor's interior network — so the buffer's
  edge-effect benefit is capped until `extra_regions` supplies the adjacent extract.
  Reinforces the §9 coverage check on the buffered extent.

### 8b. Interaction with the segment-stage boundary split

Two segment-stage steps run in sequence (`segment()`, `segment.py:559-561`):

1. `split_at_boundary` → `_split_line_by_polygon` splits each crossing way at the
   boundary, inserting a **virtual node** (id `>= virtual_id_start`) at every crossing.
   It returns both in- and out-parts.
2. **`remove_out_of_city_deadends`** (`segment.py:283`) then iteratively removes every
   out-of-city segment (midpoint outside boundary) that has a **real (non-virtual)
   endpoint of degree 1** — `s < virtual_id_start and node_degree[s] == 1` — unpeeling
   dead-end chains until stable. Virtual crossing nodes are never dead-end candidates, so
   a segment **bridging two crossings (an excursion) is preserved**.

`neighborhood._clip_lengths` separately clips **length/mileage** to the boundary.

**Effective semantics (confirmed in code):** clip at the boundary, drop one-way exits
that dangle outside, and **preserve out-and-back excursions** — not keep-every-tail. The
only outside geometry that survives without bridging two crossings is an isolated outside
*cycle* (no degree-1 node), which is an unreachable component and inert for connectivity.

Consequences for the buffer:

- **`split_at_boundary` AND `remove_out_of_city_deadends` must both receive the ANALYSIS
  (unbuffered) boundary** (they share the `boundary` arg in `segment()`), so excursions,
  dead-end removal, and mileage are all measured against the scoring line while buffer-zone
  roads remain usable in the graph. (Handing them the buffered polygon would push the
  split/dead-end line outward and wrongly retain + count buffer roads.)
- **The buffer's benefit is to enable more excursions.** At `buffer_m = 0` a connector
  lying entirely outside the boundary between two crossings is excluded (no node inside →
  whole way dropped), so that out-and-back path does not exist. A `buffer_m > 0` pulls it
  in, and because it reconnects at two crossings it is retained — delivering the
  edge-effect fix through the *existing* mechanism.
- **No new keep-vs-truncate rule needed.** Non-returning tails are already clipped/inert,
  so a buffer does **not** bloat the graph with long dangling limbs — the
  `complete_ways` over-reach concern is moot. Keep the current excursion-preserving
  semantics; the buffer composes with them.

### 9. Advisory auto-detector (guard, not auto-fetch)

- **Coverage extent = `analysis_boundary ⊕ network_buffer_m`** — the buffered clip
  extent, computed once and used both to clip and to check coverage. Both drivers of
  expansion feed it: an override/convex-hull that reaches out, *and* any non-zero
  network buffer (including whatever the existing mechanism produces).
- Compute touched regions from that extent intersected with region polygons. **The
  Geofabrik `index-v1.json` is the single region-polygon source** (fetched and cached):
  US Geofabrik extracts follow state lines, so a touched `us/<state>` extract ↔ a touched
  state is exact, and one "touched states" computation drives all three US sources. No
  separate bundled US-states layer is needed — census (FIPS) and LODES (abbr) only ever
  needed the *identifier list*, which the extract slugs already give via the crosswalk
  (`_US_STATE_SLUGS` name→slug, `region_coverage._US_STATE_SLUG_TO_FIPS` slug→FIPS,
  `_FIPS_TO_ABBR` FIPS→abbr). *(Implementation: `bikescore_bna.region_coverage`. The
  spec's earlier "bundled US-states layer" was dropped as redundant.)*
- The check runs **before** clipping, and is **gated to when expansion is possible**
  (`network_buffer_m > 0`, `convex_hull`, `override_geometry`, or a non-empty
  `extra_regions`) so the default single-region path never fetches the index. Coverage is
  tested by **difference**: `uncovered = extent − ⋃(acquired region polygons)`; if a
  non-trivial fraction of the extent (> 0.5 %, absorbing Geofabrik border slivers) is
  uncovered, the regions whose polygons intersect that gap are the missing ones.
- Action is **advisory**: if the extent needs regions not in the acquired set
  (home + `extra_regions`), raise an actionable error naming them (*"add [maryland,
  virginia] to extra_regions"*). No surprise downloads. If the index cannot be fetched
  (offline), the guard logs a warning and proceeds rather than failing acquire.
- Robustness notes: US is clean (three sources align on state lines; Geofabrik US
  extracts follow state boundaries). The detector is **US-scoped** (`parent == "us"`
  extracts); non-US census/LODES are synthetic and never expand, and non-US OSM
  multi-region relies on explicit `extra_regions` without the geometric assertion.

### 10. Phase-2 tests

- Two-state border fixture: assert a route **exists across the seam** after merge.
- Census/LODES row concat across two states de-duplicates on GEOID.
- Auto-detector: an extent straddling a region line reports the expected missing
  regions; an interior extent reports none.

---

## Config schema (new)

A `boundary:` sub-config on `BNAConfig`:

```
boundary:
  fill_holes: bool = False
  keep_largest_part: bool = False
  convex_hull: bool = False
  clip_shape: "box" | "circle" | None = None  # region-restrict shape (∩ boundary)
  clip_size_m: float | None = None            # box side / circle diameter, centered on centroid
  override_geometry: path | bbox | None = None  # SOURCE replacement, not a transform
  network_buffer_m: float = 0.0         # 0.0 = exact clip (default parity); >0 opt-in,
                                        #   expands extent → triggers coverage check (§9)
extra_regions: list[str] = []           # Geofabrik/state identifiers for multi-region
```

`make_valid` is unconditional (not a field). CLI surfacing of these is a follow-up;
this pass wires config + `city.toml`.

## Deferred

- `--auto-regions` (flip the advisory detector into fetch+merge).
- Fully automatic region detection without an `extra_regions` fallback.
- Explicit box/radius center coordinates (centroid-only for now).
- Topology simplification, inward erosion, subtract-area, union-of-municipalities.

## Out of scope / non-goals

- Changing what gets **scored**: excluded blocks stay out of scoring; parity with the
  default output is preserved when no transform is configured.
