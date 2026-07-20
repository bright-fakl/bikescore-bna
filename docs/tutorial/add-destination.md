# Add a destination

Access scores measure how many everyday destinations riders can reach on the low-stress
network. The destination types — schools, groceries, parks, transit, … — come from a
**catalog**, matched against OSM tags. You extend access scoring by adding a catalog
entry, no code required.

## How a destination is defined

A catalog entry names a destination type, the OSM tags that identify it, and how it
participates in scoring:

```yaml
destinations:
  - name: bike_shops
    category: opportunity
    match:
      shop: bicycle
```

`match` is an OSM tag matcher (a value, a list of values, or `true` for "tag present").
`category` places the destination in one of the scoring categories used by the
`neighborhood` stage. The full matcher and catalog reference is under
[Destination catalogs](../reference/destinations.md).

## Add it via a scenario

Overlay the catalog from a scenario YAML and run it:

```yaml
type: sparse
version: 1
description: Count bike shops as an opportunity destination.
config:
  destinations:
    catalog:
      - name: bike_shops
        category: opportunity
        match:
          shop: bicycle
```

```console
$ bikescore-score score ./aspen-colorado --scenario with-bike-shops.yaml
```

The `destinations` stage will cluster and locate the new type, and the `scores` /
`neighborhood` stages will fold it into the opportunity category. See
[Destinations](../how-it-works/destinations.md) for how clustering and access counting
work.
