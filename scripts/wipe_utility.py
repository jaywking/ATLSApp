# Standard library imports
import json
import os
import logging
import argparse
import sys
from pathlib import Path
from functools import partial
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Callable

# Third-party imports
from dotenv import load_dotenv

# Local application imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from . import notion_utils as nu
from .fetch_medical_facilities import _discover_facility_slots

# --- Configuration ---
MAX_WORKERS = 10  # Number of parallel requests to make. Adjust based on your network.
MASTER_DB_VARS = ["LOCATIONS_MASTER_DB", "MEDICAL_FACILITIES_DB"]

# --- Helper Functions ---

def clear_screen():
    """Clears the console screen."""
    os.system("cls" if os.name == "nt" else "clear")

def get_production_tables():
    """Loads the production tables configuration from the JSON file."""
    try:
        with open(project_root / 'notion_tables.json', 'r', encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("\n❌ [ERROR] 'notion_tables.json' not found.")
        print("Please run '[1] Sync Production Tables from Notion' from the main menu first.")
        return None

def get_master_databases(var_names: List[str]) -> List[Tuple[str, str]]:
    """Fetches master database IDs and names from Config for wiping."""
    databases = []
    print("Fetching database names for confirmation...")
    for var in var_names:
        db_id = getattr(Config, var, None)
        if not db_id:
            raise RuntimeError(f"Missing database ID in .env file for: {var}")
        try:
            db_info = nu.get_database(db_id)
            name = db_info.get("title", [{}])[0].get("plain_text", db_id)
            databases.append((db_id, name))
        except Exception as e:
            print(f"⚠️  Could not fetch name for {db_id}: {e}")
            databases.append((db_id, f"UNKNOWN DATABASE ({db_id})"))
    return databases

def process_pages_in_parallel(worker_func: Callable, pages: List[Dict], task_description: str):
    """Generic function to process Notion pages concurrently with a progress bar."""
    total = len(pages)
    if total == 0:
        print(f"✅ Database is already {task_description}ed or empty.")
        return

    # Use tqdm for a user-friendly progress bar
    try:
        from tqdm import tqdm
        use_tqdm = True
    except ImportError:
        use_tqdm = False

    print(f"Found {total} pages to {task_description}.")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(worker_func, page['id']) for page in pages]
        if use_tqdm:
            for future in tqdm(as_completed(futures), total=total, desc=f"{task_description.capitalize()}ing"):
                try:
                    future.result()
                except Exception as e:
                    print(f"\n  - ❌ Error processing a page: {e}")
        else:
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()  # Raise any exceptions from the thread
                    print(f"\r  - {task_description.capitalize()}... {i}/{total}", end="")
                except Exception as e:
                    print(f"\n  - ❌ Error processing a page: {e}")
    print(f"\n✅ {task_description.capitalize()} complete for this database.")

def confirm_action(action_description: str, items: List[str]) -> bool:
    """Prompts the user for confirmation before performing a destructive action."""
    print("\n" + "!"*50)
    print("! DANGER: THIS IS A DESTRUCTIVE OPERATION".center(50))
    print("!"*50)
    print(f"\nThis script will {action_description} the following databases:")
    for item in items:
        print(f"  - {item}")
    confirm = input("\nTo proceed, type 'YES' and press Enter: ").strip()
    if confirm != "YES":
        print("\n❌ Operation cancelled. No changes were made.")
        return False
    return True

# --- Core Logic Functions ---

def wipe_database(db_id: str, db_name: str, dry_run: bool = False):
    """Wipes all entries from a single database."""
    print(f"\n🗑️  Wiping '{db_name}'...")
    try:
        pages_to_delete = nu.query_database(db_id)
        if dry_run:
            total = len(pages_to_delete)
            if total == 0:
                print("✅ Database is already empty.")
                return
            print(f"[DRY-RUN] Found {total} pages to wipe in '{db_name}'.")
            for page in pages_to_delete:
                print(f"  - [DRY-RUN] Would wipe page ID: {page['id']}")
            print(f"\n✅ [DRY-RUN] Wipe complete for this database.")
        else:
            process_pages_in_parallel(nu.archive_page, pages_to_delete, "delete")
    except Exception as e:
        print(f"\n  - ❌ An error occurred while wiping '{db_name}': {e}")

def _validate_reset_schema(db_id: str):
    """
    Inspects the database schema to ensure it has the required properties and types
    for a reset operation. Exits if validation fails.
    """
    print("  - Validating database schema for reset...")
    expected_schema = {
        "Status": "status",
        "ProdLocID": "title",
        "Place_ID": "rich_text",
        "Latitude": "rich_text",
        "Longitude": "rich_text",
        "Location Master": "relation",
    }
    try:
        db_info = nu.get_database(db_id)
        actual_props = db_info.get("properties", {})
        errors = []
        for prop_name, expected_type in expected_schema.items():
            if prop_name not in actual_props:
                errors.append(f"    - Missing required property: '{prop_name}'")
            elif actual_props[prop_name]["type"] != expected_type:
                errors.append(f"    - Property '{prop_name}' has wrong type. Expected '{expected_type}', but found '{actual_props[prop_name]['type']}'.")
        if errors:
            sys.exit("\n❌ Schema validation failed!\nPlease correct the properties in your Notion database or the script:\n" + "\n".join(errors))
    except Exception as e:
        sys.exit(f"\n❌ Could not retrieve or validate database schema. Error: {e}")

def reset_database(db_id: str, db_name: str, dry_run: bool = False):
    """Resets specific fields in a production-specific database."""
    print(f"\n🔄  Resetting '{db_name}'...")
    try:
        pages_to_reset = nu.query_database(db_id)
        if dry_run:
            total = len(pages_to_reset)
            if total == 0:
                print("✅ Database is already empty.")
                return
            print(f"[DRY-RUN] Found {total} pages to reset in '{db_name}'.")
            for page in pages_to_reset:
                print(f"  - [DRY-RUN] Would reset page ID: {page['id']}")
            print(f"\n✅ [DRY-RUN] Reset complete for this database.")
        else:
            _validate_reset_schema(db_id)

            payload_to_reset = {
                "Status": nu.format_status(Config.STATUS_ON_RESET),
                "ProdLocID": {"title": []},
                "Place_ID": {"rich_text": []},
                "Latitude": {"rich_text": []},
                "Longitude": {"rich_text": []},
                "Location Master": {"relation": []}
            }
            reset_worker = partial(nu.update_page, properties_payload=payload_to_reset)
            process_pages_in_parallel(reset_worker, pages_to_reset, "reset")
    except Exception as e:
        print(f"\n  - ❌ An error occurred while resetting '{db_name}': {e}")

def run_wipe_medical_facilities_db(dry_run: bool = False):
    """Menu action to wipe only the Medical Facilities database."""
    try:
        db_id = Config.MEDICAL_FACILITIES_DB
        if not db_id:
            raise RuntimeError("Missing database ID in .env file for: MEDICAL_FACILITIES_DB")
        db_info = nu.get_database(db_id)
        db_name = db_info.get("title", [{}])[0].get("plain_text", db_id)
        if confirm_action("wipe", [f"'{db_name}' (Medical Facilities)"]):
            wipe_database(db_id, db_name, dry_run=dry_run)
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")

def clear_facility_links_from_master(dry_run: bool = False):
    """Clears all facility relation links from the Locations Master DB."""
    db_id = Config.LOCATIONS_MASTER_DB
    if not db_id:
        print("\n❌ LOCATIONS_MASTER_DB not configured in .env file.")
        return

    try:
        db_info = nu.get_database(db_id)
        db_name = db_info.get("title", [{}])[0].get("plain_text", db_id)
    except Exception as e:
        print(f"\n❌ Could not fetch info for Locations Master DB: {e}")
        return

    if not confirm_action("clear all facility links from", [f"'{db_name}' (Locations Master)"]):
        return

    try:
        pages_to_clear = nu.query_database(db_id)
        total_pages = len(pages_to_clear)
        if total_pages == 0:
            print("\n✅ No locations found in the master database. Nothing to do.")
            return

        # Ask user how many to clear
        num_to_clear_str = input(f"\nFound {total_pages} master locations. How many do you want to clear links for? (Press Enter for ALL): ").strip()
        if num_to_clear_str and num_to_clear_str.isdigit():
            num_to_clear = int(num_to_clear_str)
            pages_to_process = pages_to_clear[:num_to_clear]
        else:
            pages_to_process = pages_to_clear

        print(f"\n🔄  Clearing facility links from {len(pages_to_process)} location(s) in '{db_name}'...")

        facility_slots = _discover_facility_slots(db_id)
        if not facility_slots:
            print("✅ No facility slots (like 'UC1', 'ER') found in the database schema. Nothing to clear.")
            return

        payload_to_clear = {slot: {"relation": []} for slot in facility_slots}
        clear_worker = partial(nu.update_page, properties_payload=payload_to_clear)

        process_pages_in_parallel(clear_worker, pages_to_process, "clear links")

    except Exception as e:
        print(f"\n  - ❌ An error occurred while clearing links: {e}")

# --- Menu Action Functions ---

def run_full_wipe_and_reset(dry_run: bool = False):
    """Wipes master DBs and resets all production DBs."""
    try:
        master_dbs = get_master_databases(MASTER_DB_VARS)
        prod_dbs = get_production_tables() or {}
        
        all_db_names = [name for _, name in master_dbs] + list(prod_dbs.keys())
        if not confirm_action("wipe and/or reset", all_db_names):
            return

        print("\n" + "-"*20 + " WIPE IN PROGRESS " + "-"*20)
        for db_id, db_name in master_dbs:
            wipe_database(db_id, db_name, dry_run=dry_run)

        print("\n" + "-"*20 + " RESET IN PROGRESS " + "-"*20)
        for db_name, db_id in prod_dbs.items():
            reset_database(db_id, db_name, dry_run=dry_run)

        print("\n" + "-"*55)
        print("✅ All specified databases have been wiped and reset.")
    except Exception as e:
        print(f"\n❌ An unexpected error occurred during full wipe: {e}")

def select_and_wipe_prod_db(dry_run: bool = False):
    """Menu for wiping a single production database."""
    prod_tables = get_production_tables()
    if not prod_tables:
        return
    table_list = list(prod_tables.items())
    _select_and_run(table_list, wipe_database, "Select a Production Table to WIPE", dry_run=dry_run)

def select_and_reset_prod_db(dry_run: bool = False):
    """Menu for resetting a single production database."""
    prod_tables = get_production_tables()
    if not prod_tables:
        return
    table_list = list(prod_tables.items())
    _select_and_run(table_list, reset_database, "Select a Production Table to RESET", dry_run=dry_run)

def _select_and_run(items: List[Tuple[str, str]], action_func: Callable, title: str, dry_run: bool = False):
    """Generic menu for selecting an item and running an action on it."""
    clear_screen()
    print(f"─{title:─^38}─")
    for i, (name, _) in enumerate(items):
        print(f"  [{i+1}] {name}")
    print("\n  [q] Cancel and return to menu")
    print("─" * 40)

    choice_str = input("Enter your choice: ").strip()
    if choice_str.lower() == 'q':
        print("\nOperation cancelled.")
        return

    try:
        choice_idx = int(choice_str) - 1
        if not (0 <= choice_idx < len(items)):
            raise IndexError
        
        name, db_id = items[choice_idx]
        if confirm_action(f"{action_func.__name__.replace('_', ' ')}", [name]):
            logging.warning(f"Confirmation PASSED for '{name}'. Proceeding.")
            action_func(db_id, name, dry_run=dry_run)
        else:
            logging.warning(f"Confirmation FAILED for '{name}'. Operation cancelled.")
    except (ValueError, IndexError):
        print("\nInvalid choice.")

def main():
    """Displays the wipe utility sub-menu and handles user choices."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=project_root / 'wipe_utility.log',
        filemode='a'  # Append to the log file
    )
    logging.info("Wipe Utility session started.")

    parser = argparse.ArgumentParser(description="A utility to wipe or reset Notion databases.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any changes to Notion."
    )
    args = parser.parse_args()

    if args.dry_run:
        print("\n*** DRY RUN MODE ENABLED: No changes will be made to Notion. ***\n")
    if not Config.NOTION_TOKEN:
        print("❌ [ERROR] NOTION_TOKEN not found in .env file. Cannot proceed.")
        return

    while True:
        clear_screen()
        print("─" * 40)
        print("      Wipe Data Utility (DANGER)")
        print("─" * 40)
        print("[1] Wipe & Reset ALL Test Data (Master + Productions)")
        print("[2] Wipe a specific Production Table")
        print("[3] Wipe the Medical Facilities DB")
        print("[4] Clear Medical Facility Links from Master DB")
        print("[5] Reset a specific Production Table")
        print("[6] Back to Main Menu")
        print("─" * 40)
        
        choice = input("Enter your choice: ").strip()
        
        if choice == '1':
            clear_screen()
            run_full_wipe_and_reset(dry_run=args.dry_run)
            input("\nPress Enter to return to the wipe menu...")
        elif choice == '2':
            select_and_wipe_prod_db(dry_run=args.dry_run)
            input("\nPress Enter to return to the wipe menu...")
        elif choice == '3':
            run_wipe_medical_facilities_db(dry_run=args.dry_run)
            input("\nPress Enter to return to the wipe menu...")
        elif choice == '4':
            clear_facility_links_from_master(dry_run=args.dry_run)
            input("\nPress Enter to return to the wipe menu...")
        elif choice == '5':
            select_and_reset_prod_db(dry_run=args.dry_run)
            input("\nPress Enter to return to the wipe menu...")
        elif choice == '6':
            break  # Exit the sub-menu loop
        else:
            input("Invalid choice. Press Enter to try again...")
    logging.info("Wipe Utility session ended.")

if __name__ == "__main__":
    load_dotenv(dotenv_path=project_root / '.env')
    Config.setup() # Load config variables after loading .env
    main()