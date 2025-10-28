# Standard library imports
import os
import json
import sys
import argparse
from pathlib import Path
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party imports
from dotenv import load_dotenv

# Local application imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from . import notion_utils as nu

# --- SCHEMA DEFINITIONS ----------------------------------------------------

def _log(message: str, logs: Optional[list[str]] = None) -> None:
    if logs is not None:
        logs.append(message)
    else:
        print(message)

# This defines the "golden" schema that production databases should adhere to.
REQUIRED_SCHEMA = {
    "LocationsMasterID": {"type": "relation", "rename_from": "Location Master"},
    "Latitude":          {"type": "number", "rename_from": None},
    "Longitude":         {"type": "number", "rename_from": None},
    "Status":            {"type": "status", "rename_from": None},
    "Full Address":      {"type": "rich_text", "rename_from": None},
    "Place_ID":          {"type": "rich_text", "rename_from": None},
    "ProdLocID":         {"type": "title", "rename_from": None},
    "Practical Name":    {"type": "rich_text", "rename_from": None},
}

def fix_database_schema(db_id: str, db_name: str, actual_props: dict, logs: Optional[list[str]] = None) -> bool:
    """Attempts to automatically fix common schema inconsistencies."""
    update_payload = {"properties": {}}
    fixed_something = False

    for prop_name, spec in REQUIRED_SCHEMA.items():
        expected_type = spec["type"]
        
        # Handle renames (e.g., "Location Master" -> "LocationsMasterID")
        rename_from = spec.get("rename_from")
        if prop_name not in actual_props and rename_from and rename_from in actual_props:
            _log(f"  - [FIX] Renaming property '{rename_from}' to '{prop_name}' in '{db_name}'.", logs)
            update_payload["properties"][rename_from] = {"name": prop_name}
            # Update actual_props for subsequent checks in this run
            actual_props[prop_name] = actual_props.pop(rename_from)
            fixed_something = True

        # Handle type changes (e.g., rich_text -> number)
        if prop_name in actual_props and actual_props[prop_name]["type"] != expected_type:
            _log(f"  - [FIX] Changing type of '{prop_name}' to '{expected_type}' in '{db_name}'.", logs)
            update_payload["properties"][prop_name] = { "type": expected_type, expected_type: {} }
            fixed_something = True

    if fixed_something:
        nu.update_database(db_id, update_payload)
        _log(f"  - ✅ Schema for '{db_name}' has been updated.", logs)
    
    return fixed_something

# ─── SETUP ──────────────────────────────────────────────────────────────────

def ensure_status_options(db_id: str, db_name: str, autofix: bool = False, db_schema: Optional[dict] = None, logs: Optional[list[str]] = None) -> None:
    """Checks a database for required status options and optionally adds them if missing."""
    if db_schema is None:
        db_schema = nu.get_database(db_id)
    status_prop = db_schema.get("properties", {}).get("Status", {})

    if status_prop.get("type") == "status":
        existing_options = status_prop.get("status", {}).get("options", [])
        option_names_case_insensitive = {opt["name"].upper() for opt in existing_options}

        required_statuses = [Config.STATUS_ON_RESET, Config.STATUS_AFTER_MATCHING]
        missing_options = []
        for req_status in required_statuses:
            if req_status and req_status.upper() not in option_names_case_insensitive:
                missing_options.append(req_status)

        if missing_options:
            if autofix:
                new_options_to_add = [{"name": name} for name in missing_options]
                updated_options = existing_options + new_options_to_add

                update_payload = {
                    "properties": {
                        "Status": {
                            "status": {
                                "options": updated_options
                            }
                        }
                    }
                }
                nu.update_database(db_id, update_payload)
                _log(f"  - [AUTO-FIX] Added missing Status options to '{db_name}'.", logs)
            else:
                error_msg = (
                    f"\n❌ Configuration Error in Notion for database '{db_name}' (ID: {db_id}):\n"
                    f"   The 'Status' property is missing the following required option(s): {', '.join(missing_options)}\n"
                    f"   Please add these options manually or run this script with the --autofix-status flag."
                )
                raise RuntimeError(error_msg)
        else:
            _log(f"  - [CHECK] Status options already complete for '{db_name}'.", logs)

def _process_production_page(prod_page: dict, autofix_status: bool = False, autofix_schema: bool = False) -> tuple[Optional[str], Optional[str], list[str]]:
    """
    Processes a single production page from the master DB, returning its table key, db_id, and log messages.
    """
    logs: list[str] = []
    props = prod_page.get("properties", {})

    abbreviation_prop = props.get(Config.PROD_MASTER_ABBR_PROP, {}).get("rich_text", [])
    abbreviation = abbreviation_prop[0].get("plain_text", "").strip() if abbreviation_prop else None

    db_id_prop = props.get(Config.PROD_MASTER_LINK_PROP, {})
    db_id = None
    prop_type = db_id_prop.get("type")

    if prop_type == "rich_text":
        text_content = db_id_prop.get("rich_text", [])
        if text_content:
            db_id = text_content[0].get("plain_text", "").strip()
    elif prop_type == "url":
        db_id = (db_id_prop.get("url") or "").strip() or None

    if not abbreviation:
        _log(f"  - ⚠️  Skipping page {prod_page['id']}: Property '{Config.PROD_MASTER_ABBR_PROP}' is empty or missing.", logs)
        return None, None, logs
    if not db_id:
        _log(f"  - ⚠️  Skipping page {prod_page['id']} ('{abbreviation}'): Property '{Config.PROD_MASTER_LINK_PROP}' is empty or missing.", logs)
        return None, None, logs

    if "/" in db_id:
        db_id = db_id.split('/')[-1].split('?')[0]

    if len(db_id.replace('-', '')) != 32:
        _log(f"  - ❌ Invalid DB ID found for '{abbreviation}': '{db_id}'. Skipping.", logs)
        return None, None, logs

    try:
        db_name = f"{abbreviation}_Locations"
        db_info = nu.get_database(db_id)
        actual_props = db_info.get("properties", {})
        ensure_status_options(db_id, db_name, autofix=autofix_status, db_schema=db_info, logs=logs)
        if autofix_schema:
            fix_database_schema(db_id, db_name, actual_props, logs=logs)
    except Exception as e:
        _log(f"  - ❌ Error accessing/fixing database for '{abbreviation}' (ID: {db_id}). Error: {e}", logs)
        return None, None, logs

    return f"{abbreviation}_Locations", db_id, logs

def main() -> None:
    """
    Queries the Productions Master DB and overwrites notion_tables.json
    with the results.
    """
    parser = argparse.ArgumentParser(description="Sync production tables from a Notion master database and verify/fix schemas.")
    parser.add_argument(
        "--autofix-status",
        action="store_true",
        help="Automatically add missing 'Status' options to production databases."
    )
    parser.add_argument(
        "--autofix-schema",
        action="store_true",
        help="Automatically fix common schema issues like incorrect property names or types."
    )
    args = parser.parse_args()

    if not Config.PRODUCTIONS_DB_ID:
        print("\n❌ ERROR: 'PRODUCTIONS_DB_ID' not found in your configuration (.env file).")
        return

    print("🔄  Fetching productions from Notion to update notion_tables.json...")

    productions = nu.query_database(Config.PRODUCTIONS_DB_ID)
    if not productions:
        print("⚠️  No productions found in the specified database. No changes made.")
        return

    new_table_map = {}
    max_workers = min(8, len(productions)) or 1
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, prod_page in enumerate(productions):
            future = executor.submit(_process_production_page, prod_page, args.autofix_status, args.autofix_schema)
            futures[future] = idx
    ordered_results = [None] * len(productions)
    for future in as_completed(futures):
        idx = futures[future]
        try:
            ordered_results[idx] = future.result()
        except Exception as exc:
            ordered_results[idx] = (None, None, [f"  - ❌ Unexpected error processing production: {exc}"])
    for result in ordered_results:
        if result is None:
            continue
        table_key, db_id, logs = result
        for line in logs:
            print(line)
        if table_key and db_id:
            new_table_map[table_key] = db_id
            print(f"  - Found: {table_key} -> {db_id}")

    if not new_table_map:
        print("❌ No valid productions found to write to file.")
        return

    output_path = project_root / "notion_tables.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(new_table_map, f, indent=2, sort_keys=True)
    print(f"\n✅  Successfully updated '{output_path.name}'!")

if __name__ == "__main__":
    load_dotenv(dotenv_path=project_root / '.env')
    Config.setup() # Load config variables after loading .env
    main()