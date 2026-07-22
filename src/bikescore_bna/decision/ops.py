"""Closed operator set for the decision DSL (design-review Appendix A.3).

Eleven operators, one vectorized pandas mask builder each. No expressions, no
``eval`` — this is what keeps the model safe *and* statically analyzable.

Every builder takes a ``pd.Series`` (the field column, already materialized by the
evaluator) plus the clause value and returns a boolean ``pd.Series`` aligned to the
input index. NaN handling matches the legacy ``Condition``/``Range`` semantics:
``eq``/``in``/the numeric comparisons are all False on NaN; ``is_null``/``absent``
are True on NaN.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

# Operators that take no ``value`` (unary predicates).
UNARY_OPS: frozenset[str] = frozenset({"present", "absent", "is_null"})

# Operators whose ``value`` is a collection (list / set / tuple).
SET_OPS: frozenset[str] = frozenset({"in", "not_in"})

# Operators whose ``value`` is a single scalar compared numerically.
NUMERIC_OPS: frozenset[str] = frozenset({"lt", "le", "gt", "ge"})


def _as_list(value: Any) -> list:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _op_eq(col: pd.Series, value: Any) -> pd.Series:
    return col == value


def _op_ne(col: pd.Series, value: Any) -> pd.Series:
    return col != value


def _op_in(col: pd.Series, value: Any) -> pd.Series:
    return col.isin(_as_list(value))


def _op_not_in(col: pd.Series, value: Any) -> pd.Series:
    return ~col.isin(_as_list(value))


def _op_present(col: pd.Series, value: Any = None) -> pd.Series:
    return col.notna()


def _op_absent(col: pd.Series, value: Any = None) -> pd.Series:
    return col.isna()


def _op_is_null(col: pd.Series, value: Any = None) -> pd.Series:
    return col.isna()


def _numeric(col: pd.Series) -> pd.Series:
    """Coerce to float so comparisons work on object columns; non-numeric → NaN."""
    if col.dtype == object or col.dtype == bool:
        return pd.to_numeric(col, errors="coerce")
    return col


def _op_lt(col: pd.Series, value: Any) -> pd.Series:
    return _numeric(col) < value


def _op_le(col: pd.Series, value: Any) -> pd.Series:
    return _numeric(col) <= value


def _op_gt(col: pd.Series, value: Any) -> pd.Series:
    return _numeric(col) > value


def _op_ge(col: pd.Series, value: Any) -> pd.Series:
    return _numeric(col) >= value


OPS: dict[str, Callable[..., pd.Series]] = {
    "eq": _op_eq,
    "ne": _op_ne,
    "in": _op_in,
    "not_in": _op_not_in,
    "present": _op_present,
    "absent": _op_absent,
    "is_null": _op_is_null,
    "lt": _op_lt,
    "le": _op_le,
    "gt": _op_gt,
    "ge": _op_ge,
}

#: The closed op set as a frozenset, for validation.
OP_NAMES: frozenset[str] = frozenset(OPS)


def apply_op(op: str, col: pd.Series, value: Any) -> pd.Series:
    """Return the boolean mask for ``op`` over ``col`` (NaN-safe).

    Raises ``ValueError`` for an unknown operator.
    """
    fn = OPS.get(op)
    if fn is None:
        raise ValueError(f"unknown operator {op!r}; closed set is {sorted(OP_NAMES)}")
    mask = fn(col, value)
    # np.select / boolean indexing need a real bool Series with no NaN.
    return mask.fillna(False).astype(bool)


def match_scalar(op: str, actual: Any, value: Any) -> bool:
    """Row-level (scalar) evaluation of a single clause — used by matchers.

    Mirrors :func:`apply_op` for one value (an OSM tag lookup). ``actual`` is
    ``None`` when the tag is absent.
    """
    is_null = actual is None or (isinstance(actual, float) and np.isnan(actual))
    if op == "present":
        return not is_null
    if op in ("absent", "is_null"):
        return is_null
    if is_null:
        # eq/in/numeric are all False on a missing value; ne/not_in are True.
        return op in ("ne", "not_in")
    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "in":
        return actual in _as_list(value)
    if op == "not_in":
        return actual not in _as_list(value)
    try:
        num = float(actual)
    except (TypeError, ValueError):
        return False
    if op == "lt":
        return num < value
    if op == "le":
        return num <= value
    if op == "gt":
        return num > value
    if op == "ge":
        return num >= value
    raise ValueError(f"unknown operator {op!r}; closed set is {sorted(OP_NAMES)}")


# ── Transform functions (Phase 32b) ──────────────────────────────────────────
#
# Vectorized Series → Series functions for use as $apply values in the DSL.
# All functions: NaN/None input → NaN/pd.NA output (propagate).


def parse_width_ft(s: pd.Series) -> pd.Series:
    """Parse OSM width tag Series to float64 feet. NaN/None → NaN."""
    def _one(v: Any) -> float:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        v = str(v).strip()
        m = re.match(r"(\d+)'(?:(\d+)\")?", v)
        if m:
            return float(m.group(1)) + float(m.group(2) or 0) / 12
        lower = v.lower()
        if "ft" in lower:
            n = re.search(r"\d+\.?\d*", v)
            return float(n.group()) if n else np.nan
        if "m" in lower:
            n = re.search(r"\d+\.?\d*", v)
            return float(n.group()) * 3.28084 if n else np.nan
        # bare number < 20 treated as metres
        n = re.search(r"\d+\.?\d*", v)
        if n:
            num = float(n.group())
            if num < 20:
                return num * 3.28084
        return np.nan

    return s.map(_one).astype("float64")


def parse_speed_mph(s: pd.Series, bare_unit: str = "mph") -> pd.Series:
    """Parse OSM maxspeed tag Series to Int64 mph. NaN/None → pd.NA."""
    def _one(v: Any) -> Any:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return pd.NA
        digits = re.search(r"\d+(?:\.\d+)?", str(v))
        if not digits:
            return pd.NA
        n = float(digits.group())
        lower = str(v).lower()
        if "mph" in lower and "kmph" not in lower:
            return int(n)
        if "kmph" in lower or "km" in lower:
            return int(round(n / 1.609 / 5) * 5)
        if bare_unit == "mph":
            return int(n)
        return int(round(n / 1.609 / 5) * 5)

    return s.map(_one).astype("Int64")


def parse_int(s: pd.Series) -> pd.Series:
    """Extract first integer from string Series. NaN/None → pd.NA."""
    def _one(v: Any) -> Any:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return pd.NA
        m = re.search(r"\d+", str(v))
        return int(m.group()) if m else pd.NA

    return s.map(_one).astype("Int64")


def count_pipes(s: pd.Series) -> pd.Series:
    """Count '|' separators + 1 in string Series. NaN/None → pd.NA."""
    def _one(v: Any) -> Any:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return pd.NA
        return str(v).count("|") + 1

    return s.map(_one).astype("Int64")


def parse_width_int(s: pd.Series) -> pd.Series:
    """Parse OSM width tag Series to truncated-integer feet (float64). NaN/None → NaN.

    Mirrors the legacy ``impute._parse_width``: PostgreSQL stored ``width_ft`` as
    ``INT`` so fractional feet are *truncated* (not rounded). Unlike
    :func:`parse_width_ft` (float64, no truncation), this keeps the historical
    INT-cast semantics for the road-width ``width_ft`` attribute. The result is
    stored as float64 to match the ``width_ft`` catalog type and the former impute
    column dtype, but every value is an exact truncated integer.
    """
    def _one(v: Any) -> float:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        v = str(v).strip()
        m = re.match(r"(\d+)'(?:(\d+)\")?", v)
        if m:
            return float(int(float(m.group(1)) + float(m.group(2) or 0) / 12))
        lower = v.lower()
        if "ft" in lower:
            n = re.search(r"\d+\.?\d*", v)
            return float(int(float(n.group()))) if n else np.nan
        if "m" in lower:
            n = re.search(r"\d+\.?\d*", v)
            return float(int(float(n.group()) * 3.28084)) if n else np.nan
        n = re.search(r"\d+\.?\d*", v)
        if n:
            num = float(n.group())
            if num < 20:
                return float(int(num * 3.28084))
        return np.nan

    return s.map(_one).astype("float64")


def parse_int_half(s: pd.Series) -> pd.Series:
    """Parse first integer then ceiling-divide by 2. NaN/None → pd.NA."""
    parsed = parse_int(s)

    def _ceil_half(v: Any) -> Any:
        if pd.isna(v):
            return pd.NA
        return math.ceil(int(v) / 2.0)

    return parsed.map(_ceil_half).astype("Int64")


TRANSFORM_FNS: dict[str, Callable] = {
    "parse_width_ft": parse_width_ft,
    "parse_speed_mph": parse_speed_mph,
    "parse_int": parse_int,
    "count_pipes": count_pipes,
    "parse_int_half": parse_int_half,
    "parse_width_int": parse_width_int,
}
