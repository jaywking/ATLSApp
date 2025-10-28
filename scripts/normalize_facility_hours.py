"""
Utility to normalize opening-hour fields on existing medical facility pages.

Some legacy entries store values like "Tuesday: 8:00 AM – 8:00 PM" inside properties
that are already scoped to a specific weekday (e.g., "Tuesday Hours"). This script
strips the redundant weekday prefix so the value is just "8:00 AM – 8:00 PM".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

# Ensure project imports resolve cleanly when the script is executed directly.
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts import notion_utils as nu
from scripts.fetch_medical_facilities import DAY_TO_PROP


PREFIX_PATTERN_TEMPLATE = r"^{day}\s*[:\-–—]\s*"


def _get_rich_text(props: Dict, key: str) -> str:
    """Safely pull the first rich_text value."""
    arr = props.get(key, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""


def _get_title(props: Dict, key: str) -> str:
    """Retrieve the first title field for logging."""
    arr = props.get(key, {}).get("title", [])
    return arr[0].get("plain_text", "") if arr else ""


def _normalize_hours(day: str, value: str) -> str:
    """Remove a leading weekday prefix if present."""
    stripped = value.strip()
    if not stripped:
        return value

    pattern = re.compile(PREFIX_PATTERN_TEMPLATE.format(day=re.escape(day)), re.IGNORECASE)
    normalized = pattern.sub("", stripped, count=1).strip()
    return normalized if normalized != value else normalized


def normalize_facility_hours(*, dry_run: bool = False) -> None:
    """Iterate every facility page and normalize weekday hour fields."""
    if not Config.MEDICAL_FACILITIES_DB:
        print("[ERROR] MEDICAL_FACILITIES_DB is not configured.")
        return

    facility_pages = nu.query_database(Config.MEDICAL_FACILITIES_DB)
    if not facility_pages:
        print("[INFO] No facility pages found.")
        return

    updated_pages = 0
    total_fields = 0

    for page in facility_pages:
        props = page.get("properties", {})
        updates: Dict[str, Dict] = {}
        per_day_changes: Dict[str, tuple[str, str]] = {}

        for day, prop_name in DAY_TO_PROP.items():
            existing = _get_rich_text(props, prop_name)
            if not existing:
                continue

            normalized = _normalize_hours(day, existing)
            if normalized != existing:
                updates[prop_name] = nu.format_rich_text(normalized)
                per_day_changes[prop_name] = (existing, normalized)

        if not updates:
            continue

        updated_pages += 1
        total_fields += len(updates)
        title = _get_title(props, "MedicalFacilityID") or page.get("id", "")

        if dry_run:
            print(f"[DRY-RUN] {title}:")
            for prop_name, (before, after) in per_day_changes.items():
                print(f"  - {prop_name}: '{before}' -> '{after}'")
            continue

        nu.update_page(page["id"], updates)

    mode_label = "(dry-run)" if dry_run else ""
    print(f"[INFO] Normalized {total_fields} hour field(s) across {updated_pages} facility page(s) {mode_label}".strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize facility opening hours to drop weekday prefixes.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without updating Notion.")
    args = parser.parse_args()

    load_dotenv(dotenv_path=project_root / ".env")
    Config.setup()

    normalize_facility_hours(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
