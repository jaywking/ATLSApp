"""Ensure production location tables include audit columns.

Adds `Created time` and `Last edited time` properties to every production
database listed in `notion_tables.json`, skipping those that already have the
columns.

Run: `python scripts/add_audit_columns.py`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import sys

project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts import notion_utils as nu

NOTION_MAP_FILE = project_root / "notion_tables.json"
AUDIT_COLUMNS = {
    "Created time": {"created_time": {}},
    "Last edited time": {"last_edited_time": {}},
}


def _load_production_map() -> Dict[str, str]:
    if not NOTION_MAP_FILE.is_file():
        raise FileNotFoundError("notion_tables.json not found. Run the sync first.")
    with NOTION_MAP_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("notion_tables.json has unexpected structure.")
    return data


def run() -> None:
    Config.setup()
    table_map = _load_production_map()
    if not table_map:
        print("[WARN] notion_tables.json is empty. Nothing to do.")
        return

    added_total = 0
    skipped_total = 0

    for name, db_id in table_map.items():
        print(f"\nChecking '{name}' ({db_id})...")
        try:
            database = nu.get_database(db_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] Unable to fetch database: {exc}")
            skipped_total += 1
            continue

        current_props = database.get("properties", {})
        missing = {
            key: value
            for key, value in AUDIT_COLUMNS.items()
            if key not in current_props
        }

        if not missing:
            print("  [SKIP] Audit columns already present.")
            skipped_total += 1
            continue

        try:
            nu.update_database(db_id, {"properties": missing})
            print(f"  [OK] Added columns: {', '.join(missing.keys())}")
            added_total += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] Failed to add columns: {exc}")
            skipped_total += 1

    print("\n=== Summary ===")
    print(f"Databases processed: {len(table_map)}")
    print(f"Databases updated: {added_total}")
    print(f"Skipped/unchanged: {skipped_total}")


if __name__ == "__main__":
    run()

