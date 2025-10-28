"""Convenience script to backfill/cleanup medical facility links.

Runs the existing backfill routine from `fetch_medical_facilities` to repair
or remove orphaned relations. Use when locations are deleted or facilities
fall out of sync.

Usage: `python scripts/cleanup_orphan_facilities.py`
"""

from __future__ import annotations

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts.fetch_medical_facilities import run_backfill as run_facility_backfill


def run(dry_run: bool = False) -> None:
    Config.setup()
    run_facility_backfill(dry_run=dry_run)


if __name__ == "__main__":
    run(dry_run=False)
