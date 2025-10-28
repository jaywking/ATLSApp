# Standard library imports
import os
import importlib
import sys
import json
import time
import random
import logging
import re
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from math import radians, cos, sin, sqrt, atan2
from collections import defaultdict
import csv
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
import requests
from dotenv import load_dotenv
from tqdm import tqdm

# Local application imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from . import notion_utils as nu
from config import Config
from .google_utils import geocode as get_geocode, place_details as get_place_details

_MASTER_ENTRY_CACHE: dict[str, tuple[str | None, str | None]] = {}
_GEOCODE_CACHE: dict[str, dict | None] = {}


class SchemaValidationError(Exception):
    """Raised when a production database is missing required schema pieces."""
    pass

# ── ENV & CONSTANTS ────────────────────────────────────────────────────────────
DISTANCE_MATCH_THRESHOLD_METERS = 10  # Max distance to consider two points a match

# ── CSV Logging Setup ────────────────────────────────────────────────────────

LOGS_DIR = project_root / "logs"
LOG_FILE = LOGS_DIR / "process_new_locations_log.csv"
LOG_BUFFER_THRESHOLD = 200  # switch to buffered logging above this many rows

def setup_csv_logging_file():
    """Ensures the log directory and CSV file with header exist."""
    LOGS_DIR.mkdir(exist_ok=True)
    if not LOG_FILE.is_file():
        with open(LOG_FILE, 'w', newline='', encoding='utf-8-sig') as csvfile:
            fieldnames = ['timestamp', 'production_db_name', 'page_id', 'ProdLocID', 'Place_ID', 'Latitude', 'Longitude', 'master_id', 'note']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

class TqdmLoggingHandler(logging.Handler):
    """A logging handler that uses tqdm.write to avoid disrupting progress bars."""
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
    def emit(self, record):
        tqdm.write(self.format(record), file=sys.stderr)

# ─── NOTION HELPERS ───────────────────────────────────────────────────────────

def select_database() -> Tuple[str, str]:
    try:
        with open(project_root / "notion_tables.json", "r") as f:
            table_map = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("\n❌ ERROR: `notion_tables.json` is missing or corrupted.")
        print("Please run 'Sync Production Tables from Notion' (Option 1) to generate it.")
        sys.exit(1)

    # Check if the file looks like it has placeholder data from a failed sync
    if any(key == value for key, value in table_map.items()):
        print("\n⚠️ WARNING: `notion_tables.json` may contain placeholder data.")
        print("If you see errors, please run 'Sync Production Tables from Notion' (Option 1) first.")

    print("Select a production database to process:")
    for i, name in enumerate(table_map.keys(), 1):
        print(f"[{i}] {name}")
    while True:
        choice = input("> ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(table_map):
            key = list(table_map.keys())[int(choice) - 1]
            return key, table_map[key]
        print("Invalid choice.")

def get_locations_to_process(db_id: str, process_all: bool = False) -> List[Dict]:
    """Fetches all locations from the production database."""
    return nu.query_database(db_id, filter_payload=None)

def filter_locations_to_process(all_locations: List[Dict], process_all: bool) -> List[Dict]:
    """Filters the full list of locations to only those that need processing."""
    if process_all:
        return all_locations

    status_on_reset = (Config.STATUS_ON_RESET or "Ready").lower()

    def is_ready(page):
        status = page["properties"].get("Status", {}).get("status", {}).get("name", "").lower()
        place_id = _get_text_from_property(page["properties"], "Place_ID")
        return status == status_on_reset and not place_id

    return [page for page in all_locations if is_ready(page)]

def validate_database_schema(db_id: str):
    """
    Inspects the database schema to ensure it has the required properties and types
    before starting the main processing logic.
    """
    print("Validating database schema...")
    expected_schema = {
        "ProductionID": "relation",
        "ProdLocID": "title",
        "LocationsMasterID": "relation",
        "Abbreviation": "rollup",
        "Location Name": "rich_text",
        "Practical Name": "rich_text",
        "Status": "status",
        "Full Address": "rich_text",
        "Place_ID": "rich_text",
        "Latitude": "number",
        "Longitude": "number",
        "Created time": "created_time",
        "Last edited time": "last_edited_time",
    }

    try:
        db_info = nu.get_database(db_id)
        actual_props = db_info.get("properties", {})
        errors = []

        for prop_name, expected_type in expected_schema.items():
            if prop_name not in actual_props:
                errors.append(f"  - Missing required property: '{prop_name}'")
            elif actual_props[prop_name]["type"] != expected_type:
                errors.append(f"  - Property '{prop_name}' has wrong type. Expected '{expected_type}', but found '{actual_props[prop_name]['type']}'.")

        if errors:
            error_message = (
                "[ERROR] Schema validation failed!\n"
                "Please correct the properties in your Notion database:\n"
                + "\n".join(errors)
            )
            raise SchemaValidationError(error_message)
        print("  - [OK] Schema validation successful.")
    except Exception as e:
        raise SchemaValidationError(
            f"Could not retrieve or validate database schema. Error: {e}"
        ) from e

def _get_text_from_property(props: Dict, prop_name: str, prop_type: str = 'rich_text') -> str:
    """Safely extracts plain text from a Notion property."""
    prop_list = props.get(prop_name, {}).get(prop_type, [])
    return prop_list[0].get("plain_text", "") if prop_list else ""

def _get_rollup_text(props: Dict, prop_name: str) -> str:
    """Safely extracts plain text from a Notion rollup property."""
    rollup_prop = props.get(prop_name)
    if not rollup_prop or rollup_prop.get("type") != "rollup":
        return ""

    rollup_array = rollup_prop.get("rollup", {}).get("array", [])
    if not rollup_array:
        return ""

    # The first item in the array contains the rolled-up value.
    item = rollup_array[0]
    return item.get(item.get("type", ""), [{}])[0].get("plain_text", "")

def get_location_master_entries(db_id: str) -> List[Dict]:
    """Fetches and caches all entries from the Locations Master DB."""
    all_pages = nu.query_database(db_id)
    master_entries = []
    for page in all_pages:
        master_entries.append({
            "page_id": page["id"],
            "place_id": _get_text_from_property(page["properties"], "Place_ID"),
            "lat": page["properties"].get("Latitude", {}).get("number"),
            "lng": page["properties"].get("Longitude", {}).get("number"),
            "LocationsMasterID": _get_text_from_property(page["properties"], "LocationsMasterID", "title"),
            "Practical Name": _get_text_from_property(page["properties"], "Practical Name")
        })
    return master_entries

def _build_master_properties(data: Dict, details: Dict, practical_name: Optional[str] = None) -> Dict:
    """Constructs the Notion properties payload for a Locations Master entry."""
    props = {
        "LocationsMasterID": nu.format_title(data["LocationsMasterID"]),
        "Full Address":      nu.format_rich_text(data["Full Address"]),
        "Latitude":          nu.format_number(data["Latitude"]),
        "Longitude":         nu.format_number(data["Longitude"]),
        "Place_ID":          nu.format_rich_text(data["Place_ID"]),
    }
    if practical_name:
        props["Practical Name"] = nu.format_rich_text(practical_name)
    if details.get("name"):
        props["Name"] = nu.format_rich_text(details["name"])
    if details.get("url"):
        props["Google Maps URL"] = nu.format_url(details["url"])
    if details.get("vicinity"):
        props["Vicinity"] = nu.format_rich_text(details["vicinity"])
    if details.get("international_phone_number"):
        props["International Phone"] = nu.format_phone_number(details["international_phone_number"])
    if details.get("website"):
        props["Website"] = nu.format_url(details["website"])
    if details.get("types"):
        filtered_types = [t for t in details["types"] if t not in ["establishment", "point_of_interest"]]
        if filtered_types:
            props["Types"] = nu.format_multi_select(filtered_types)
    return props




def _geocode_with_cache(address: str) -> Optional[dict]:
    key = address.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    result = get_geocode(address)
    _GEOCODE_CACHE[key] = result
    return result

def _master_payload_differs(master_page: dict, new_props: dict) -> bool:
    """Return True if the Notion page lacks any of the new property values."""
    current = master_page.get("properties", {})
    for key, value in new_props.items():
        existing = current.get(key)
        if not existing:
            return True
        # Compare formatted Notion property payloads as raw dicts
        if existing != value:
            return True
    return False

def create_location_master_entry(data: Dict, details: Dict, master_db_id: str, practical_name: Optional[str] = None, dry_run: bool = False) -> str:
    """Creates a new entry in the Locations Master DB."""
    props = _build_master_properties(data, details, practical_name)

    if dry_run:
        name_for_log = practical_name or details.get("name", "Unknown Name")
        print(f"  - [DRY-RUN] Would create new master entry '{data['LocationsMasterID']}' for '{name_for_log}'.")
        return "dry-run-new-page-id"

    new_page = nu.create_page(master_db_id, props)
    return new_page["id"]

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000  # Earth radius in meters
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2)**2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2)**2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))

def _build_prodlocid_counts(pages: List[Dict]) -> defaultdict[str, int]:
    """Builds a mapping of prefix -> highest numeric suffix seen, used for sequential IDs."""
    counts = defaultdict(int)
    pattern = re.compile(r'^([A-Za-z0-9]+?)(\d+)$')
    for page in pages:
        prod_id = _get_text_from_property(page["properties"], "ProdLocID", "title").strip()
        if not prod_id:
            continue
        match = pattern.match(prod_id)
        if not match:
            continue
        prefix, number = match.groups()
        counts[prefix] = max(counts[prefix], int(number))
    return counts


def _find_production_master_entry(db_id: str, table_name: str) -> tuple[Optional[str], Optional[str]]:
    """Locate the master production entry tied to this database and return (page_id, abbreviation)."""
    if db_id in _MASTER_ENTRY_CACHE:
        return _MASTER_ENTRY_CACHE[db_id]

    production_page_id: Optional[str] = None
    production_abbr: Optional[str] = None

    try:
        db_info = nu.get_database(db_id)
        db_url = db_info.get("url")
    except Exception:
        db_url = None

    if not Config.PRODUCTIONS_MASTER_DB:
        result = (production_page_id, production_abbr)
        _MASTER_ENTRY_CACHE[db_id] = result
        return result

    try:
        master_entries = nu.query_database(Config.PRODUCTIONS_MASTER_DB)
    except Exception:
        master_entries = []

    for entry in master_entries:
        props = entry.get("properties", {})
        link_prop = props.get(Config.PROD_MASTER_LINK_PROP, {}) if Config.PROD_MASTER_LINK_PROP else {}
        link_url = link_prop.get("url") if isinstance(link_prop, dict) else None
        if db_url and link_url and link_url.rstrip('/') == db_url.rstrip('/'):
            production_page_id = entry.get("id")
            abbr_prop = props.get(Config.PROD_MASTER_ABBR_PROP, {})
            rich = abbr_prop.get("rich_text", []) if isinstance(abbr_prop, dict) else []
            if rich:
                production_abbr = rich[0].get("plain_text", "").strip() or None
            break

    if not production_abbr:
        token = table_name.replace('-', ' ').split()[0] if table_name else ''
        production_abbr = token.strip() or production_abbr

    result = (production_page_id, production_abbr or None)
    _MASTER_ENTRY_CACHE[db_id] = result
    return result


def _process_single_location(
    page: Dict,
    master_entries_by_place_id: Dict[str, Dict],
    master_entries_list: List[Dict],
    prod_id_counts: defaultdict,
    production_prefix: Optional[str],
    process_all: bool,
    log_mode: str,
    log_buffer: Optional[List[Dict]],
    shared_counters: Dict[str, int],
    csv_writer: Optional[csv.DictWriter],
    api_key: str,
    master_db_id: str,
    dry_run: bool = False,
    locks: Dict[str, threading.Lock] = None
) -> Optional[Dict]:
    """
    Processes a single location page: geocodes, generates IDs, matches/creates master entry, and updates the original page.
    Returns a dictionary with the result or None on failure.
    """
    props = page["properties"]
    log_messages = []
    page_id = page["id"]
    master_page_id: Optional[str] = None

    full_address = _get_text_from_property(props, "Full Address")
    practical_name = _get_text_from_property(props, "Practical Name").strip() # User-provided name
    parsed_practical_name = "" # Name parsed from a messy address

    # Handle cases where the business name is included in the address field
    if not practical_name and "," in full_address: # If practical name is empty but address has a comma
        parts = full_address.split(",", 1)
        # A simple heuristic: if the part before the comma is not a number, it's likely a name.
        if not parts[0].strip().isdigit():
            parsed_practical_name = parts[0].strip()
            # Use the parsed name and the rest of the string as the address to geocode
            address_to_geocode = f"{parsed_practical_name}, {parts[1].strip()}"
            log_messages.append(f"  - Info: Parsed '{parsed_practical_name}' as Practical Name from Full Address.")
        else:
            # The part before the comma is a number, so treat the whole thing as an address
            address_to_geocode = full_address
    else:
        # Default behavior: prefer the full address; fall back to practical name if needed.
        address_to_geocode = full_address or practical_name
    
    if not address_to_geocode:
        log_messages.append(f"  - ⚠️ Skipping page {page_id}: Full Address and Practical Name are both empty.")
        return {"status": "skipped", "logs": log_messages, "address": "Empty Address"}

    # Explicitly skip common placeholder values
    if address_to_geocode.strip().upper() == "TBD":
        status_name = Config.STATUS_ERROR or "Error"
        log_messages.append(
            f"  - ❌ ERROR: Address is a placeholder ('TBD'). Setting status to '{status_name}' in Notion."
        )

        # Update the status in Notion to make the data quality issue visible.
        if not dry_run:
            update_payload = {
                "Status": nu.format_status(status_name),
                "Notes": nu.format_rich_text("Automation Error: Address is a placeholder ('TBD').")
            }
            nu.update_page(page_id, update_payload)

        return {"status": "failure", "logs": log_messages, "address": address_to_geocode}

    # 1. Geocode the address
    geo = _geocode_with_cache(address_to_geocode)
    if not geo:
        log_messages.append(f"Processing: {address_to_geocode}")
        log_messages.append("  - ⚠️ Geocoding failed. Skipping.")
        return {"status": "failure", "logs": log_messages, "address": address_to_geocode}

    canonical_address = (geo.get("formatted_address") or "").strip()
    if canonical_address and canonical_address.lower() != full_address.strip().lower():
        full_address = canonical_address
        log_messages.append(f"  - Normalized address to '{canonical_address}'.")

    # Use locks to safely generate unique IDs in a multithreaded environment
    with locks["prod_id"]:
        abbr_value = _get_rollup_text(props, "Abbreviation").strip()
        if not abbr_value and production_prefix:
            abbr_value = production_prefix.strip()
        if not abbr_value:
            log_messages.append(f"Processing: {address_to_geocode}")
            log_messages.append(f"  - ⚠️ Could not determine abbreviation. Skipping. (Raw data: {props.get('Abbreviation')})")
            return {"status": "failure", "logs": log_messages, "address": address_to_geocode}
        next_id_num_for_prod = prod_id_counts[abbr_value] + 1
        prod_loc_id = f"{abbr_value}{str(next_id_num_for_prod).zfill(3)}"
        # IMPORTANT: Increment the in-memory counter to prevent duplicate IDs in the same run
        prod_id_counts[abbr_value] = next_id_num_for_prod

    # 3. Match against Master DB
    # First, try an O(1) lookup by place_id
    match = master_entries_by_place_id.get(geo["place_id"])

    # If no match, perform the slower proximity search
    if not match:
        match = next((m for m in master_entries_list if m["lat"] and haversine(geo["lat"], geo["lng"], m["lat"], m["lng"]) < DISTANCE_MATCH_THRESHOLD_METERS), None)

    if match:
        master_page_id = match["page_id"]
        log_messages.append(f"  - ✅ Found match in Master DB: {match.get('LocationsMasterID', 'Unknown ID')}")
        # If the master record is missing a Practical Name, update it from this production entry.
        if practical_name and not match.get("Practical Name"):
            if not dry_run:
                log_messages.append(f"    - Backfilling missing Practical Name on master record...")
                nu.update_page(master_page_id, {"Practical Name": nu.format_rich_text(practical_name)})
            else:
                log_messages.append(f"    - [DRY-RUN] Would backfill master record {match.get('LocationsMasterID', master_page_id)} with Practical Name: '{practical_name}'.")
            match["Practical Name"] = practical_name

        if process_all and geo.get("place_id"):
            detail_payload = get_place_details(
                geo["place_id"],
                ["name", "types", "url", "vicinity", "international_phone_number", "website"]
            ) or {}
            master_payload = {
                "LocationsMasterID": match.get("LocationsMasterID") or "",
                "Full Address": full_address,
                "Latitude": geo["lat"],
                "Longitude": geo["lng"],
                "Place_ID": geo["place_id"]
            }
            name_for_master = practical_name or parsed_practical_name or match.get("Practical Name")
            props_to_update = _build_master_properties(master_payload, detail_payload, name_for_master)
            if dry_run:
                log_messages.append(f"    - [DRY-RUN] Would refresh master record {match.get('LocationsMasterID', master_page_id)} with latest Google data.")
            else:
                master_page = nu.get_page(master_page_id)
                if _master_payload_differs(master_page, props_to_update):
                    nu.update_page(master_page_id, props_to_update)
                    log_messages.append("    - Updated master record with latest details.")
                else:
                    log_messages.append("    - Master record already up to date; skipped refresh.")
                match["lat"] = geo["lat"]
                match["lng"] = geo["lng"]
                if name_for_master:
                    match["Practical Name"] = name_for_master
                master_entries_by_place_id[geo["place_id"]] = match
    else:
        details = get_place_details(geo["place_id"], ["name", "types", "url", "vicinity", "international_phone_number", "website"])
        with locks["master"]:
            next_master_id_num = shared_counters["master_id"]
            next_master_id = f"LOC{str(shared_counters['master_id']).zfill(3)}"
            shared_counters["master_id"] += 1

            name_for_master = practical_name or parsed_practical_name

            master_page_id = create_location_master_entry(
                {"LocationsMasterID": next_master_id, "Full Address": full_address, "Latitude": geo["lat"], "Longitude": geo["lng"], "Place_ID": geo["place_id"]},
                details, master_db_id, practical_name=name_for_master, dry_run=dry_run
            )
            if not dry_run:
                log_messages.append(f"  - ✅ Created new Master DB entry: {next_master_id}")

            new_entry_for_cache = {
                "page_id": master_page_id,
                "place_id": geo["place_id"],
                "lat": geo["lat"],
                "lng": geo["lng"],
                "LocationsMasterID": next_master_id,
                "Practical Name": name_for_master
            }
            master_entries_list.append(new_entry_for_cache)
            if geo["place_id"]:
                master_entries_by_place_id[geo["place_id"]] = new_entry_for_cache

    # 4. Update the original production location page
    # Set the status to "Done", which is a known valid status for the target database.
    # This avoids issues with incorrect values in the .env file.
    status_after_match = Config.STATUS_AFTER_MATCHING or "Matched"
    google_maps_url = ""
    if 'detail_payload' in locals():
        google_maps_url = detail_payload.get("url", "")
    if not google_maps_url and 'details' in locals():
        google_maps_url = details.get("url", "") if details else ""

    update_payload = {
        "ProdLocID": nu.format_title(prod_loc_id),
        "Place_ID": nu.format_rich_text(geo.get("place_id")),
        "Latitude": nu.format_number(geo["lat"]),
        "Longitude": nu.format_number(geo["lng"]),
        "LocationsMasterID": nu.format_relation(master_page_id),
        "Status": nu.format_status(status_after_match),
        "Full Address": nu.format_rich_text(full_address),
    }
    if google_maps_url:
        update_payload["Google Maps URL"] = nu.format_url(google_maps_url)
    # If we parsed a name, update the Practical Name field in the production table as well.
    if parsed_practical_name and not practical_name:
        update_payload["Practical Name"] = nu.format_rich_text(parsed_practical_name)

    try:
        log_data = {
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "production_db_name": Config.NOTION_DATABASE_NAME,  # Assuming this is set in config.py
            "page_id": page_id,
            "ProdLocID": prod_loc_id,
            "Place_ID": geo["place_id"],
            "Latitude": geo["lat"],
            "Longitude": geo["lng"],
            "master_id": master_page_id,
            "note": "DRY-RUN" if dry_run else ("linked master" if match else "created master")
        }

        if csv_writer is not None:
            if log_mode == "buffered" and log_buffer is not None:
                with locks["csv"]:
                    log_buffer.append(log_data)
            elif log_mode != "off":
                # Use a lock to ensure thread-safe writing to the CSV file
                with locks["csv"]:
                    csv_writer.writerow(log_data) # csv_writer is now passed in

        if not dry_run:
            nu.update_page(page_id, update_payload)
            log_messages.append(f"  - ✅ Successfully updated production location.")
        else:
            log_messages.append(f"  - [DRY-RUN] Would update production location with ProdLocID: {prod_loc_id}.")
        
        return {"status": "success", "logs": log_messages, "address": address_to_geocode, "master_page_id": master_page_id}
    except Exception as e:
        log_messages.append(f"  - ❌ FAILED to update production location: {e}")
        return {"status": "failure", "logs": log_messages, "address": address_to_geocode, "master_page_id": master_page_id}

# ─── MAIN LOGIC ───────────────────────────────────────────────────────────────

def run(process_all: bool = False, dry_run: bool = False, log_mode: str = "auto") -> None:
    """Process production locations, optionally re-processing every row."""
    requested_log_mode = (log_mode or 'auto').lower()
    valid_log_modes = {'auto', 'immediate', 'buffered', 'off'}
    if requested_log_mode not in valid_log_modes:
        raise ValueError(f"Invalid log_mode '{log_mode}'. Choose from {sorted(valid_log_modes)}.")

    _GEOCODE_CACHE.clear()

    # Reset root handlers so we can reconfigure logging cleanly on repeated runs
    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        handlers=[TqdmLoggingHandler()],
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    if dry_run:
        print("\n*** DRY RUN MODE ENABLED: No changes will be made to Notion. ***")

    if not all([Config.LOCATIONS_MASTER_DB, Config.GOOGLE_MAPS_API_KEY]):
        print("\n? ERROR: Missing required environment variables.")
        print("Please ensure LOCATIONS_MASTER_DB and GOOGLE_MAPS_API_KEY are in your .env file.")
        return

    table_name, db_id = select_database()

    # Perform a pre-flight check on the database schema before proceeding
    try:
        validate_database_schema(db_id)
    except SchemaValidationError as exc:
        print(f"\n{exc}")
        print("\n[HINT] Update the database in Notion and rerun this option once the schema is fixed.")
        return

    process_all_mode = bool(process_all)

    if process_all_mode:
        print(f"\n??  WARNING: Re-processing will update every location in '{table_name}'.")
        print("This will overwrite existing geocoded data and generate new ProdLocIDs.")
        confirm = input("Are you sure you want to continue? (y/N): ").strip().lower()
        if confirm != 'y':
            print("Operation cancelled.")
            return
    else:
        print(f"\nProcessing only new ('READY') locations in '{table_name}'.")

    Config.NOTION_DATABASE_NAME = table_name  # Stash the database name for logging.

    print("Fetching all necessary data from Notion...")
    all_prod_locations = get_locations_to_process(db_id)  # Fetches all pages once
    locations_to_process = filter_locations_to_process(all_prod_locations, process_all_mode)

    master_entries_list = get_location_master_entries(Config.LOCATIONS_MASTER_DB)
    # Create a dictionary for fast O(1) lookups by place_id
    master_entries_by_place_id = {m["place_id"]: m for m in master_entries_list if m.get("place_id")}

    production_master_page_id, production_prefix = _find_production_master_entry(db_id, table_name)

    linked_count = 0
    if production_master_page_id:
        for page in locations_to_process:
            prod_rel = page.get("properties", {}).get("ProductionID", {})
            rel_items = prod_rel.get("relation", []) if isinstance(prod_rel, dict) else []
            if rel_items:
                continue
            try:
                nu.update_page(page["id"], {"ProductionID": nu.format_relation(production_master_page_id)})
                linked_count += 1
                page.setdefault("properties", {}).setdefault("ProductionID", {})["relation"] = [{"id": production_master_page_id}]
            except Exception as exc:
                logging.warning(f"Could not auto-link ProductionID for page {page.get('id')}: {exc}")
        if linked_count:
            logging.info(f"Auto-linked ProductionID for {linked_count} page(s). Refreshing data...")
            all_prod_locations = get_locations_to_process(db_id)
            locations_to_process = filter_locations_to_process(all_prod_locations, process_all_mode)

    # --- Pre-calculate IDs to avoid bugs and performance issues ---
    master_id_nums = [
        int(e["LocationsMasterID"][3:])
        for e in master_entries_list
        if e.get("LocationsMasterID", "").startswith("LOC") and e["LocationsMasterID"][3:].isdigit()
    ]
    next_master_id_num = max(master_id_nums, default=0) + 1

    prod_id_counts = _build_prodlocid_counts(all_prod_locations)

    shared_counters = {
        "master_id": next_master_id_num
    }

    total_locations = len(locations_to_process)
    print(f"Found {total_locations} location(s) to process.")

    if requested_log_mode == "auto":
        effective_log_mode = "buffered" if (not dry_run and total_locations > LOG_BUFFER_THRESHOLD) else "immediate"
    else:
        effective_log_mode = requested_log_mode

    log_buffer: Optional[List[Dict]] = [] if effective_log_mode == "buffered" else None

    locks = {
        "master": threading.Lock(),
        "prod_id": threading.Lock(),
        "print": threading.Lock(),
        "csv": threading.Lock(),
    }

    csv_context = nullcontext(None)
    if effective_log_mode != "off":
        setup_csv_logging_file()
        csv_context = open(LOG_FILE, "a", newline="", encoding="utf-8-sig")

    with csv_context as csv_file:
        fieldnames = ['timestamp', 'production_db_name', 'page_id', 'ProdLocID', 'Place_ID', 'Latitude', 'Longitude', 'master_id', 'note']
        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames) if csv_file else None

        refreshed_master_ids: set[str] = set()

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    _process_single_location,
                    page,
                    master_entries_by_place_id,
                    master_entries_list,
                    prod_id_counts,
                    production_prefix,
                    process_all_mode,
                    effective_log_mode,
                    log_buffer,
                    shared_counters,
                    csv_writer,
                    Config.GOOGLE_MAPS_API_KEY,
                    Config.LOCATIONS_MASTER_DB,
                    dry_run,
                    locks
                ): page for page in locations_to_process
            }

            for future in tqdm(as_completed(futures), total=total_locations, desc="Processing Locations"):
                try:
                    result = future.result()
                    if result and result.get("logs"):
                        with locks["print"]:
                            print(f"\n--- Processing: {result.get('address', 'Unknown Address')} ---")
                            print("\n".join(result["logs"]))
                    if result and result.get("status") == "success" and result.get("master_page_id"):
                        refreshed_master_ids.add(result["master_page_id"])
                except Exception as e:
                    print(f"\n? An unexpected error occurred in a worker thread: {e}")

        if effective_log_mode == "buffered" and log_buffer and csv_writer:
            csv_writer.writerows(log_buffer)

    print("\n?? All done!")

    if not dry_run and refreshed_master_ids:
        try:
            facility_module = importlib.import_module('scripts.fetch_medical_facilities')

            print(f"\nTriggering facility refresh for {len(refreshed_master_ids)} master location(s)...")
            facility_module.run_facility_refresh(master_ids=refreshed_master_ids)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Facility refresh failed: {exc}")


def run_reprocess(log_mode: str = "auto") -> None:
    """Convenience wrapper to re-process every location in the selected database."""
    run(process_all=True, dry_run=False, log_mode=log_mode)

def main():
    parser = argparse.ArgumentParser(
        description="Process new locations from a Notion database, geocode them, and match/create master entries."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all locations, not just those with a 'READY' status."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any changes to Notion."
    )
    parser.add_argument(
        "--log-mode",
        choices=["auto", "immediate", "buffered", "off"],
        default="auto",
        help="Control CSV logging behaviour (default: auto)."
    )
    args = parser.parse_args()
    run(process_all=args.all, dry_run=args.dry_run, log_mode=args.log_mode)


if __name__ == "__main__":
    load_dotenv(dotenv_path=project_root / '.env')
    Config.setup() # Load config variables after loading .env
    main()
