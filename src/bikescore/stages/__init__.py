"""Pipeline stages.

Phase 38d ports the walking skeleton ``parse → census → jobs → attributes → segment →
stress`` (pure compute functions + their ``StageSpec`` wrappers); 38e appends
``graph → connectivity → destinations → scores → neighborhood``. ``parse.BASE_WAY_TAGS``
is the irreducible base OSM tag set the ``attributes`` registry references while building
the default attribute registry (``BNAConfig.with_defaults()``).
"""
