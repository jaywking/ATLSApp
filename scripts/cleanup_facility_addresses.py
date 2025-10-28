"""Utility script to sanitize Medical Facilities addresses in Notion.

Removes trailing country suffixes such as ", USA" / ", United States" and
normalizes whitespace so that the Fetch Medical Facilities workflow and LHA
generation produce consistent addresses.

Usage: python scripts/cleanup_facility_addresses.py
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts import notion_utils as nu


ADDRESS_FIELDS: Tuple[str, ...] = (
    "Full Address",
    "Address",
)

PHONE_FIELDS: Tuple[str, ...] = (
    "Phone",
    "International Phone",
)

MAX_WORKERS = 6


def _get_rich_text(props: Dict, key: str) -> str:
    arr = props.get(key, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""


def _normalize(address: str) -> str:
    if not address:
        return address
    text = address.strip()
    # Collapse internal whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove trailing country suffixes (case-insensitive)
    text = re.sub(r",\s*(USA|United States)$", "", text, flags=re.IGNORECASE)
    # Remove any trailing commas introduced by removal
    text = re.sub(r",\s*$", "", text)
    return text.strip()


def _normalize_phone(value: str, *, international: bool = False) -> str:
    if not value:
        return value
    text = value.strip()
    if not text:
        return text

    digits = re.sub(r"\D", "", text)
    if not digits:
        return text

    country_code = None
    local_digits = digits

    if digits.startswith("1") and len(digits) == 11:
        country_code = "1"
        local_digits = digits[1:]
    elif text.startswith("+") and len(digits) > 10:
        country_code = digits[:-10]
        local_digits = digits[-10:]

    if len(local_digits) == 10:
        if international:
            # Prefer explicit country code when available, default to +1
            cc = country_code or "1"
            return f"+{cc} {local_digits[:3]}-{local_digits[3:6]}-{local_digits[6:]}"
        return f"({local_digits[:3]}) {local_digits[3:6]}-{local_digits[6:]}"

    if international and country_code and len(local_digits) >= 7:
        return f"+{country_code} {local_digits}"

    return text


def _process_page(page: Dict) -> tuple[bool, str | None]:
    page_id = page.get("id")
    props = page.get("properties", {})
    updates: Dict[str, Dict] = {}

    for field in ADDRESS_FIELDS:
        value = _get_rich_text(props, field)
        if not value:
            continue
        cleaned = _normalize(value)
        if cleaned != value:
            updates[field] = nu.format_rich_text(cleaned)

    for field in PHONE_FIELDS:
        value = _get_rich_text(props, field)
        if not value:
            continue
        cleaned = _normalize_phone(value, international=(field == "International Phone"))
        if cleaned != value:
            updates[field] = nu.format_rich_text(cleaned)

    if not updates:
        return False, None

    try:
        nu.update_page(page_id, updates)
        name = _get_rich_text(props, "Location Name") or _get_rich_text(props, "Name")
        fields = ", ".join(updates.keys())
        return True, f"[UPDATED] {name or page_id}: {fields}"
    except Exception as exc:  # noqa: BLE001
        return False, f"[WARN] Failed to update page {page_id}: {exc}"


def run() -> None:
    Config.setup()
    db_id = Config.MEDICAL_FACILITIES_DB
    if not db_id:
        print("[ERROR] MEDICAL_FACILITIES_DB is not configured in your .env file.")
        return

    print("Fetching Medical Facilities pages...")
    pages = nu.query_database(db_id)
    if not pages:
        print("No facilities found. Nothing to do.")
        return

    updated_count = 0
    skipped_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_process_page, page) for page in pages]
        for fut in as_completed(futures):
            updated, message = fut.result()
            if updated:
                updated_count += 1
            else:
                skipped_count += 1
            if message:
                print(message)

    print("\n--- Summary ---")
    print(f"Updated pages: {updated_count}")
    print(f"Unchanged pages: {skipped_count}")


if __name__ == "__main__":
    run()
