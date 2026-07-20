# Destination catalogs

Access scoring measures how many everyday destinations each census block can reach by a
low-stress bike route. *Which* destinations count — and how OSM tags map to them — is a
**catalog**: a list of destination types held on `config.destinations`. This page is the
reference for the catalog structure; for how the destinations stage clusters POIs and
links them to blocks see [How destinations work](../how-it-works/destinations.md), and for
a worked addition see [Add a destination](../tutorial/add-destination.md).

## The standard catalog

The bundled `default` scenario carries the 13 standard BNA destination types across three
scoring categories (opportunity, core services, recreation) — schools, colleges,
universities, supermarkets, doctors, dentists, hospitals, pharmacies, parks, trails,
community centers, social services, and transit. Each type contributes to its category's
score with a declared weight; the categories combine into the block's `overall_score`.
See [Scoring](../how-it-works/scoring.md) for the weighting.

## Anatomy of a destination type

Each entry pairs OSM matchers with scoring metadata:

```yaml
- name: schools
  display_name: K-12 Schools
  type: poi                       # poi | area | network_path (trails)
  node_match:                     # tag matchers, in the shared decision-DSL form
    any:
    - all:
      - { field: amenity, op: in, value: [school, kindergarten] }
  area_match:  { … }              # matcher for polygon features
  way_match:   { any: [] }        # matcher for way features
  exclude_match: { any: [] }      # features to drop even if matched
  clustering_mode: no_cluster     # no_cluster | poly_cluster | point_cluster
  clustering_tolerance_m: 0       # merge radius for point_cluster / poly_cluster
  scoring_category: opportunity   # opportunity | core_services | recreation
  category_weight: 0.35           # share of the category's score
  scoring: { first: 0.3, second: 0.2, third: 0.2, max_score: 1.0 }
  score_id: opportunity_k12_education
  human_explanation: K12 schools
  enabled: true
```

- **Matchers** (`node_match` / `area_match` / `way_match` / `exclude_match`) use the same
  decision-DSL matcher form as the [stress](stress-rules.md) and attribute rules — an
  `any`-of-`all` group of `{field, op, value}` conditions. Node/area/way matchers select
  from the three OSM geometry kinds; `exclude_match` removes false positives.
- **Clustering** collapses nearby POIs of the same type into one destination:
  `no_cluster` keeps each feature, `point_cluster` / `poly_cluster` merge features within
  `clustering_tolerance_m`.
- **Scoring** — `scoring_category` and `category_weight` place the type in a category and
  set its share; the `scoring` block (`first`/`second`/`third`/`max_score`) is the
  diminishing-returns curve for reaching one, two, three of that type.
- **`type: network_path`** is the special trails pseudo-destination: off-network paths
  measured by length rather than clustered POIs.

## Compact form

On disk a catalog may also be written in a **compact** form that omits empty matchers and
default fields; the loader expands it to the canonical structure above. Either form is
accepted wherever a catalog is read. Because a *complete* scenario embeds the full catalog,
customising destinations means editing the catalog inside a scenario (or deriving a new
scenario) — the loader validates every type's matchers and scoring metadata on read, and
`config.validate()` rejects a type whose `scoring_category` is not a declared category.
