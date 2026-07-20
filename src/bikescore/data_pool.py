"""Content-addressed source-data pool for a single-city output directory.

``acquire_city`` writes each acquired file here under a content-addressed name so a
re-download of the same bytes always lands on the same filename (the acquisition date
lives in the ledger, not the name). This is the DB-free slice of bna-core's
``data_pool``: only ``store_file`` + ``update_ledger`` are core concerns. The
dataset-set / stage-meta garbage-collection helpers (``refresh_ledger`` /
``unreferenced_files``) are workspace bookkeeping and stay in the orchestration app.
"""

from __future__ import annotations

import hashlib
import json
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


def update_ledger(data_dir: Path, filename: str, metadata: dict) -> None:
    """Add or update an entry in ``data_dir/meta.json``. Never removes existing entries."""
    ledger_path = data_dir / "meta.json"
    ledger = {}
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text())
        except Exception:
            pass
    entry = {**metadata, "present": (data_dir / filename).exists()}
    ledger[filename] = entry
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    tmp.replace(ledger_path)
