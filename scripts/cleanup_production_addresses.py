"""Normalize production location addresses across all production databases.

Removes trailing country suffixes (", USA" / ", United States"), collapses
whitespace, and updates the `Full Address` property for every location in each
production database referenced by `notion_tables.json`.

Run: `python scripts/cleanup_production_addresses.py`
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict

import sys

project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts import notion_utils as nu


FULL_ADDRESS_FIELD = "Full Address"
MAX_WORKERS = 6
NOTION_MAP_FILE = project_root / "notion_tables.json"


def _normalize(address: str) -> str:
    text = address.strip()
    if not text:
        return text
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",\s*(USA|United States)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*$", "", text)
    return text.strip()


def _get_full_address(props: Dict) -> str:
    arr = props.get(FULL_ADDRESS_FIELD, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""


def _process_page(page: Dict) -> tuple[bool, str | None]:
    page_id = page.get("id")
    props = page.get("properties", {})
    current = _get_full_address(props)
    if not current:
        return False, None

    cleaned = _normalize(current)
    if cleaned == current:
        return False, None

    try:
        nu.update_page(page_id, {FULL_ADDRESS_FIELD: nu.format_rich_text(cleaned)})
        name = props.get("Practical Name", {}).get("rich_text", [])
        practical = name[0].get("plain_text", "") if name else ""
        label = practical or _get_full_address(props) or page_id
        return True, f"[UPDATED] {label}"
    except Exception as exc:  # noqa: BLE001
        return False, f"[WARN] Failed to update page {page_id}: {exc}"


def _load_production_map() -> Dict[str, str]:
    if not NOTION_MAP_FILE.is_file():
        raise FileNotFoundError("notion_tables.json not found. Run sync first.")
    with NOTION_MAP_FILE.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("notion_tables.json has unexpected structure.")
    return data


def run() -> None:
    Config.setup()
    table_map = _load_production_map()
    if not table_map:
        print("[WARN] notion_tables.json is empty. Nothing to clean.")
        return

    total_updated = 0
    total_pages = 0

    for prod_name, db_id in table_map.items():
        print(f"\nProcessing production '{prod_name}' ({db_id})...")
        pages = nu.query_database(db_id)
        if not pages:
            print("  - No pages found.")
            continue

        updated = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(_process_page, page) for page in pages]
            for fut in as_completed(futures):
                did_update, message = fut.result()
                if message:
                    print(f"  {message}")
                if did_update:
                    updated += 1

        total_pages += len(pages)
        total_updated += updated
        print(f"  Summary for {prod_name}: {updated} updated / {len(pages)} total")

    print("\n=== Overall Summary ===")
    print(f"Databases processed: {len(table_map)}")
    print(f"Pages scanned: {total_pages}")
    print(f"Pages updated: {total_updated}")


if __name__ == "__main__":
    run()

