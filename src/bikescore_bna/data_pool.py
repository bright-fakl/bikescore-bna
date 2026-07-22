"""Content-addressed source-data pool for a single-city output directory.

``acquire_city`` writes each acquired file here under a content-addressed name so a
re-download of the same bytes always lands on the same filename. Only ``store_file`` is
a core concern — a pure content-addressed blob write. Dataset metadata, provenance
ledgers, and garbage collection are workspace bookkeeping and live in the app.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def _sha256_short(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def store_file(data_dir: Path, file_type: str, src_path: Path, ext: str | None = None) -> str:
    """Move *src_path* into *data_dir* under a content-addressed name; return the name.

    The name is ``{file_type}-{sha256[:12]}{ext}`` — purely content-addressed so that
    re-downloading identical bytes on any day always yields the same filename. If the
    destination already exists (same content), the source is discarded.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    short_hash = _sha256_short(src_path)
    suffix = ext or src_path.suffix
    name = f"{file_type}-{short_hash}{suffix}"
    dst = data_dir / name
    if not dst.exists():
        shutil.move(str(src_path), dst)
    else:
        src_path.unlink(missing_ok=True)
    return name
