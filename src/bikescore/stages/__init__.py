"""Pipeline stages. Ported in Phase 38d (parse→stress) and 38e (graph→neighborhood).

Phase 38b (foundation) seeds only ``parse.BASE_WAY_TAGS`` here — the irreducible base
OSM tag set that ``attributes`` references while registering the default attribute
registry (``BNAConfig.with_defaults()``). The full ``parse`` stage replaces the stub
module in 38d.
"""
