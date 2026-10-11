"""Replay entry points and shared owned-artifact helpers."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .grading import query_metrics
from .pipeline import seed_run


def atomic_json(path: Path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def disk_bytes(root):
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total
