# Data acquisition

Before it can score a city, `bikescore-bna` needs data *about* that city: its
boundary, its roads, and how many people live and work there. The **acquire**
step gathers these files — five raw inputs, database-free — and returns the
`dict[str, Path]` that [`score_city`](../reference/api.md) consumes.

```python
from bikescore_bna import acquire_city, CityIdentity

city = CityIdentity(name="Aspen", slug="aspen-colorado",
                    region="Colorado", country="united states", fips_code="0803620")
inputs = acquire_city(city, "./data")
# {"osm": …, "boundary": …, "census": …, "lodes_main": …, "lodes_aux": …}
```

## The five inputs

| input | source | notes |
|---|---|---|
| `boundary` | US Census (via `pygris`) for US cities; Nominatim otherwise | GeoJSON polygon in EPSG:4326 |
| `osm` | [Geofabrik](https://download.geofabrik.de) regional extract, **clipped to the boundary** | see [clipping](../differences/deviations.md#clipping-approaches) |
| `census` | US Census 2020 blocks (via `pygris`), filtered to the boundary | population; US only |
| `lodes_main`, `lodes_aux` | US Census LODES8 OD files | employment; US only |

Non-US cities receive a Nominatim boundary and the OSM clip only — there is no census or
LODES data, and the population/employment scores are correspondingly empty.

## The shared regional-PBF cache

Geofabrik publishes OSM extracts per **state / country**, not per city. Acquisition
downloads the regional PBF once into a shared cache (`~/.bikescore-bna/pbf/` by default) and
clips it to each city boundary. A second city in the same state reuses the cached
download. Each cached PBF carries a `.meta.json` sidecar recording its source URL,
timestamp, size, and checksum; a re-acquire is a cache hit unless you pass `force=True`.

Relocate the cache by passing `pbf_cache_dir=` to `acquire_city` or setting the
`BIKESCORE_PBF_CACHE` environment variable. `bikescore-bna` resolves this default itself and
does not read any global settings file — cache placement is left to the caller (or to
whatever tool drives acquisition).

## Clipping

The regional PBF is trimmed to the city boundary before parsing. When the `osmium`
command-line tool is available it is used directly; otherwise a pure-Python `pyosmium`
fallback produces byte-equivalent results more slowly. Clipping semantics — and how they
differ from the brokenspoke-analyzer reference — are documented under
[Intentional deviations](../differences/deviations.md#clipping-approaches).

## Boundary manipulation

By default the analysis uses the fetched city polygon verbatim. The optional
[`boundary`](../reference/config.md#boundary-transforms) config reshapes it into an
**analysis boundary** — the polygon `parse`, `census`, and `segment` consume — while the
original fetched boundary is always kept alongside for provenance. `acquire_city` persists
**both** (`boundary` = original, `analysis_boundary` = derived); with no transform
configured the two are identical and the derived one reuses the same file, so default output
is byte-for-byte unchanged.

Transforms run once here, at acquire time, in a fixed order (`make_valid` →
`keep_largest_part` → `fill_holes` → box/circle clip). Doing it upstream keeps the OSM clip
and the census-block filter consistent — a newly-included enclave gets both its roads and
its blocks. The census stage also emits an inert
[excluded-block layer](../reference/output-files.md#targets) so blocks it drops (water-only,
or mostly outside the boundary) are distinguishable from genuinely-missing data. See
[Configuration → Boundary transforms](../reference/config.md#boundary-transforms) for every
field.

### Multi-region acquisition

Most transforms only shrink the extent or fill interior holes, so they stay within the
already-downloaded state extract. Three — `convex_hull`, `override_geometry`, and a non-zero
`network_buffer_m` — can push the extent across a state line (the classic case is
Washington DC, where any outward growth crosses into Maryland or Virginia). When that
happens acquire fetches every region listed in `extra_regions` and stitches them together:

- **OSM** — each Geofabrik state extract is downloaded and combined with `osmium merge`, an
  exact ID-based union. Because Geofabrik extracts are reference-complete, a border-crossing
  way appears in full in every extract it touches and deduplicates to a single connected
  way, so the two road networks join with no seam gaps. (The `osmium` CLI is required when
  more than one region is fetched.)
- **Census / LODES** — per-state blocks / OD files, concatenated and de-duplicated on GEOID.

A **coverage guard** runs before any download: it intersects the analysis + network-buffer
extent with the Geofabrik region polygons and, if the extent reaches a region not in the
acquired set (home + `extra_regions`), raises an actionable error naming the missing regions
rather than silently downloading them. Preview the plan without downloading anything via
[`acquire --dry-run`](../reference/cli.md#acquire); if the Geofabrik index can't be fetched
(offline), the guard warns and proceeds. It is US-scoped — non-US census/LODES are synthetic
and never expand.

## The `InputProvider` seam

`acquire_city` is a thin wrapper over an `InputProvider` — the US census/LODES provider
by default. Other geographies (or a custom consolidated dataset) plug in by implementing
the protocol and passing it explicitly:

```python
class InputProvider(Protocol):
    def acquire(self, city, out_dir, *, force=False) -> dict[str, Path]: ...

acquire_city(city, "./data", provider=MyProvider())
```

The pipeline treats the input names as opaque keys; the provider is what gives them
meaning. This is the plug point for non-US data or a prebuilt network.

## Reproducibility

Upstream sources evolve — the Geofabrik extract, census vintages, and LODES years all
change over time — so a re-acquire is **not** guaranteed byte-identical to a past run.
For reproducible scoring, keep the acquired input files (they are content-addressed by
name) rather than re-acquiring.

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
