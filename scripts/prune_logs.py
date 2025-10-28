#!/usr/bin/env python3
"""
Utility script to prune old log/artefact files so the repo stays lightweight.

Defaults:
    - Target directories: logs/, project root (for CSV/ZIP artefacts)
    - Retention: 30 days
    - Extension filter: .csv, .log, .zip

Usage:
    python -m scripts.prune_logs
    python -m scripts.prune_logs --days 14 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = [
    PROJECT_ROOT / "logs",
    PROJECT_ROOT,
]
DEFAULT_EXTS = {".csv", ".log", ".zip"}


def _iter_candidate_files(paths: Iterable[Path], extensions: set[str]) -> Iterable[Path]:
    """Yield files within the provided directories that match the extension filter."""
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            if base.suffix.lower() in extensions:
                yield base
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in extensions:
                yield path


def prune(days: int, include: list[Path], extensions: set[str], dry_run: bool) -> list[Path]:
    """
    Delete (or report) files older than the retention window.

    Returns a list of files that were deleted (or would be in dry-run).
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    removed: list[Path] = []

    for file_path in _iter_candidate_files(include, extensions):
        try:
            mtime = dt.datetime.fromtimestamp(file_path.stat().st_mtime, dt.timezone.utc)
        except OSError:
            continue
        if mtime > cutoff:
            continue

        removed.append(file_path)
        if dry_run:
            print(f"[DRY-RUN] Would delete {file_path}")
        else:
            try:
                file_path.unlink()
                print(f"Deleted {file_path}")
            except OSError as exc:
                print(f"Failed to delete {file_path}: {exc}")

    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune log/artefact files older than the retention window.")
    parser.add_argument("--days", type=int, default=30, help="Retention window in days (default: 30).")
    parser.add_argument(
        "--paths",
        nargs="*",
        type=Path,
        default=DEFAULT_DIRS,
        help="Directories/files to scan (default: logs/ and project root for artefacts).",
    )
    parser.add_argument(
        "--ext",
        nargs="*",
        default=sorted(DEFAULT_EXTS),
        help="File extensions to target (default: .csv .log .zip).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show files that would be deleted without removing them.")
    args = parser.parse_args()

    extensions = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in args.ext}
    removed = prune(days=args.days, include=args.paths, extensions=extensions, dry_run=args.dry_run)

    if removed:
        summary = "would be deleted" if args.dry_run else "deleted"
        print(f"\n{len(removed)} file(s) {summary}.")
    else:
        print("\nNo files matched the retention criteria.")


if __name__ == "__main__":
    main()
