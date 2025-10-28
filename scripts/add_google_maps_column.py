"""Add a 'Google Maps URL' property to all production location databases.

Reads `notion_tables.json`, checks each production-specific database, and
ensures a URL property named exactly 'Google Maps URL' exists. Skips databases
that already have the property.

Run: `python scripts/add_google_maps_column.py`
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
TARGET_PROPERTY = "Google Maps URL"


def _load_production_map() -> Dict[str, str]:
    if not NOTION_MAP_FILE.is_file():
        raise FileNotFoundError("notion_tables.json not found. Run the sync first.")
    with NOTION_MAP_FILE.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("notion_tables.json has unexpected format.")
    return data


def run() -> None:
    Config.setup()
    table_map = _load_production_map()
    if not table_map:
        print("[WARN] No production databases listed. Nothing to do.")
        return

    added = 0
    skipped = 0

    for name, db_id in table_map.items():
        print(f"\nChecking '{name}' ({db_id})...")
        try:
            db = nu.get_database(db_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] Could not fetch database: {exc}")
            skipped += 1
            continue

        properties = db.get("properties", {})
        if TARGET_PROPERTY in properties:
            print("  [SKIP] Property already present.")
            skipped += 1
            continue

        try:
            nu.update_database(db_id, {"properties": {TARGET_PROPERTY: {"url": {}}}})
            print("  [OK] Added 'Google Maps URL' property.")
            added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] Failed to add property: {exc}")
            skipped += 1

    print("\n=== Summary ===")
    print(f"Databases processed: {len(table_map)}")
    print(f"Properties added: {added}")
    print(f"Skipped/unchanged: {skipped}")


if __name__ == "__main__":
    run()

