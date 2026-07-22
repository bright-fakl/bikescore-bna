"""Import-direction guard — the load-bearing rule of the Phase 38 split.

The scoring *core* (`bikescore-bna`) must never import from the orchestration layer
(`bikescore`). The dependency direction is app -> core, one-way. A violation here
is an architecture regression, not a style nit — content-addressed reuse, the web UI,
and the run store all depend on core staying free of them.

This scans source (and test) files statically so it fails even if the app package is not
installed in the environment.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"

# Any of these module roots would invert the dependency direction.
_FORBIDDEN_ROOTS = {"bikescore"}


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_ROOTS:
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _FORBIDDEN_ROOTS:
                hits.append(node.module or "")
    return hits


def test_core_never_imports_app() -> None:
    violations: dict[str, list[str]] = {}
    for path in _python_files(_SRC):
        hits = _forbidden_imports(path)
        if hits:
            violations[str(path.relative_to(_REPO_ROOT))] = hits
    assert not violations, (
        "bikescore-bna (core) must not import the orchestration layer "
        f"({sorted(_FORBIDDEN_ROOTS)}); found: {violations}"
    )
