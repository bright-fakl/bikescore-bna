# Installation

`bikescore` requires **Python 3.11+**.

## Install the package

```console
$ pip install bikescore
```

or, in a `uv`-managed project:

```console
$ uv add bikescore
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

Everything works without it — the binary only affects acquisition speed, never results.

## Verify

```console
$ bikescore-score scenarios
default
$ python -c "import bikescore; print(bikescore.__version__)"
0.1.0
```
