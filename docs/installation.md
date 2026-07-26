# Installation

`bikescore-bna` requires **Python 3.11+**.

## Install the package

```console
$ pip install bikescore-bna
```

or, in a `uv`-managed project:

```console
$ uv add bikescore-bna
```

This pulls the scientific stack it depends on (GeoPandas, Shapely, pyproj, SciPy,
NumPy, pandas/polars, PyArrow) plus `pygris` (US census geometry) and `requests`
(data acquisition). The library carries **no** web or database dependencies — it runs
entirely in-process.

## Optional: the `osmium` binary (recommended)

OSM clipping (trimming the regional PBF to the city boundary) shells out to the
[`osmium-tool`](https://osmcode.org/osmium-tool/) command-line program when it is on
`PATH`. It is substantially faster than the pure-Python fallback (`pyosmium`, ~8×
slower), which is used automatically when `osmium` is not found.

```console
# Debian/Ubuntu
$ sudo apt install osmium-tool
# macOS
$ brew install osmium-tool
```

For a single region, everything works without it — the binary only affects
acquisition speed, never results.

!!! warning "Required for multi-region acquisition"
    Acquiring a city whose analysis extent crosses a state line — anything with
    `extra_regions` — merges the Geofabrik extracts with `osmium merge`, which has
    **no pyosmium fallback**. That path requires the `osmium` CLI and raises a clear
    error if it is missing. See
    [Data acquisition → Multi-region acquisition](how-it-works/data-acquisition.md#multi-region-acquisition).

## Verify

```console
$ bikescore-bna scenarios
default
$ python -c "import bikescore_bna; print(bikescore_bna.__version__)"
0.1.0
```
