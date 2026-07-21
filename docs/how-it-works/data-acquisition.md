# Data acquisition

`bikescore` scores a city from five raw inputs. [`acquire_city`](../reference/api.md)
fetches all of them, database-free, and returns the `dict[str, Path]` that
[`score_city`](../reference/api.md) consumes.

```python
from bikescore import acquire_city, CityIdentity

city = CityIdentity(name="Aspen", slug="aspen-colorado",
                    region="Colorado", country="united states", fips_code="0803620")
inputs = acquire_city(city, "./data")
# {"osm": …, "boundary": …, "census": …, "lodes_main": …, "lodes_aux": …}
```

## The five inputs

| input | source | notes |
|---|---|---|
| `boundary` | US Census (via `pygris`) for US cities; Nominatim otherwise | GeoJSON polygon in EPSG:4326 |
| `osm` | [Geofabrik](https://download.geofabrik.de) regional extract, **clipped to the boundary** | see [clipping](deviations.md#clipping-approaches) |
| `census` | US Census 2020 blocks (via `pygris`), filtered to the boundary | population; US only |
| `lodes_main`, `lodes_aux` | US Census LODES8 OD files | employment; US only |

Non-US cities receive a Nominatim boundary and the OSM clip only — there is no census or
LODES data, and the population/employment scores are correspondingly empty.

## The shared regional-PBF cache

Geofabrik publishes OSM extracts per **state / country**, not per city. Acquisition
downloads the regional PBF once into a shared cache (`~/.bikescore/pbf/` by default) and
clips it to each city boundary. A second city in the same state reuses the cached
download. Each cached PBF carries a `.meta.json` sidecar recording its source URL,
timestamp, size, and checksum; a re-acquire is a cache hit unless you pass `force=True`.

Relocate the cache by passing `pbf_cache_dir=` to `acquire_city` or setting the
`BIKESCORE_PBF_CACHE` environment variable. `bikescore` resolves this default itself and
does not read any global settings file — cache placement is left to the caller (or to
whatever tool drives acquisition).

## Clipping

The regional PBF is trimmed to the city boundary before parsing. When the `osmium`
command-line tool is available it is used directly; otherwise a pure-Python `pyosmium`
fallback produces byte-equivalent results more slowly. Clipping semantics — and how they
differ from the brokenspoke-analyzer reference — are documented under
[Known deviations](deviations.md#clipping-approaches).

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
name) rather than re-acquiring. Parity validation therefore pins a **frozen** set of
Aspen inputs rather than a live download.
