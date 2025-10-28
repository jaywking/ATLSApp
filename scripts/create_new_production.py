import logging
import sys
from pathlib import Path
from notion_client.errors import APIResponseError

# Add project root ('c:\\Utils\\LocationsSync') to path to allow for clean imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

from config import Config
from scripts import notion_utils as nu

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _build_status_property_definition() -> dict:
    """
    Returns the canonical Status property definition.

    Notion currently prevents seeding Status options via the public API, so we
    request a bare Status property. The calling code follows up with reminders
    if the expected options are absent.
    """
    return {"status": {}}


def get_locations_db_schema(locations_master_db_id: str) -> dict:
    """
    Returns the standard schema for a new production locations database.
    This ensures consistency across all created databases.
    """
    return {
        # --- Relations & Identifiers ---
        "ProductionID": {
            "relation": {
                "database_id": Config.PRODUCTIONS_MASTER_DB,
                "single_property": {}
            }
        },
        "ProdLocID": {"title": {}},  # The official Title property, populated by automation
        "LocationsMasterID": {
            "relation": {
                "database_id": locations_master_db_id,
                "single_property": {}
            }
        },
        "Abbreviation": {
            "rollup": {
                "relation_property_name": "ProductionID",
                "rollup_property_name": "Abbreviation",
                "function": "show_original"
            }
        },

        # --- Naming & Status ---
        "Location Name": {"rich_text": {}},
        "Practical Name": {"rich_text": {}},
        "Status": _build_status_property_definition(),

        # --- Address & Coordinates ---
        "Full Address": {"rich_text": {}},
        "Google Maps URL": {"url": {}},
        "Place_ID": {"rich_text": {}},
        "Latitude": {"number": {}},
        "Longitude": {"number": {}},

        # --- Audit metadata ---
        "Created time": {"created_time": {}},
        "Last edited time": {"last_edited_time": {}}
    }

def configure_status_property(database_id: str) -> None:
    """
    Ensures the target database has a Status property with the expected options.
    Groups are configured so that new rows default to the Ready state.
    """
    desired_options = [
        {"name": Config.STATUS_ON_RESET, "color": "default"},
        {"name": Config.STATUS_AFTER_MATCHING, "color": "green"},
        {"name": Config.STATUS_ERROR, "color": "red"}
    ]

    try:
        db_obj = nu.get_database(database_id)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Unable to inspect database %s before configuring Status: %s", database_id, exc)
        return

    properties = db_obj.get("properties", {})
    status_prop = properties.get("Status")
    if status_prop and status_prop.get("type") != "status":
        logging.warning(
            "Database %s already has a 'Status' property of type '%s'; skipping auto-configuration.",
            database_id,
            status_prop.get("type")
        )
        return

    if status_prop is None:
        try:
            nu.update_database(
                database_id,
                {"properties": {"Status": {"status": {"options": desired_options}}}}
            )
            logging.info("Created missing Status property on database %s", database_id)
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "Unable to add a Status property to database %s automatically: %s",
                database_id,
                exc
            )
            print("[REMINDER] Add a 'Status' property (type: Status) in Notion.")
            return

    try:
        db_obj = nu.get_database(database_id)
        status_prop = db_obj.get("properties", {}).get("Status", {})
        if not status_prop or status_prop.get("type") != "status":
            print("[REMINDER] Add a 'Status' property (type: Status) in Notion.")
            return
        option_map = {
            opt.get("name"): opt.get("id")
            for opt in status_prop.get("status", {}).get("options", [])
            if isinstance(opt, dict)
        }
        ready_id = option_map.get(Config.STATUS_ON_RESET)
        matched_id = option_map.get(Config.STATUS_AFTER_MATCHING)
        error_id = option_map.get(Config.STATUS_ERROR)

        groups_payload = []
        if ready_id:
            groups_payload.append({"name": "To-do", "color": "default", "option_ids": [ready_id]})
        if matched_id:
            groups_payload.append({"name": "Complete", "color": "green", "option_ids": [matched_id]})
        if error_id:
            groups_payload.append({"name": "Needs Attention", "color": "red", "option_ids": [error_id]})

        if groups_payload:
            nu.update_database(
                database_id,
                {"properties": {"Status": {"status": {"groups": groups_payload}}}}
            )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Unable to configure Status groups on database %s: %s", database_id, exc)

def validate_config(required_vars: list[str]) -> bool:
    """Checks if all required variables are present in the Config."""
    missing_vars = [var for var in required_vars if not getattr(Config, var, None)]
    if missing_vars:
        logging.error("Missing one or more required environment variables.")
        print("\n[ERROR] Missing one or more required environment variables in your .env file:")
        for var in missing_vars:
            print(f"  - {var}")
        return False
    return True

def create_locations_database(production_name: str, abbreviation: str, parent_page_id: str, locations_master_db_id: str) -> dict:
    """
    Creates a new locations database in Notion for the given production.
    Returns the API response object for the new database.
    """
    db_title = f"{abbreviation.upper()}_Locations"
    print(f"\n[INFO] Attempting to create a new locations database '{db_title}' for '{production_name}'...")
    db_schema = get_locations_db_schema(locations_master_db_id)

    new_db_response = nu.create_database(
        parent_page_id=parent_page_id,
        title=[{"type": "text", "text": {"content": db_title}}],
        schema=db_schema
    )

    new_db_id = new_db_response["id"]
    logging.info(f"Successfully created new database '{db_title}' with ID: {new_db_id}")
    print(f"[INFO] Successfully created new locations database '{db_title}'.")

    description_text = f"Production: {production_name}\nDatabase ID: {new_db_id}"
    try:
        nu.update_database(
            new_db_id,
            {"description": [{"type": "text", "text": {"content": description_text}}]}
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Unable to set description for database %s: %s", new_db_id, exc)

    # Ensure the Status property exists with the expected options and default grouping.
    configure_status_property(new_db_id)

    # --- Post-create: verify Status options and remind if Notion rejected them ---
    try:
        db_obj = nu.get_database(new_db_id)
        properties = db_obj.get("properties", {})
        status_prop = properties.get("Status")

        if status_prop is None:
            print("[REMINDER] Add a 'Status' property (type: Status) in Notion.")
            print("  After adding the property, create these options:"
                  f" {Config.STATUS_ON_RESET}, {Config.STATUS_AFTER_MATCHING}, {Config.STATUS_ERROR}.")
            raise ValueError("Status property missing")

        existing = [opt.get("name") for opt in status_prop.get("status", {}).get("options", []) if isinstance(opt, dict)]
        required = [Config.STATUS_ON_RESET, Config.STATUS_AFTER_MATCHING, Config.STATUS_ERROR]
        missing = [name for name in required if name not in (existing or [])]
        if missing:
            print("[REMINDER] Add these Status options in Notion:")
            for name in missing:
                print(f"   - {name}")
            print("  Open the database in Notion, edit the 'Status' property, and add the missing options.")
        else:
            print("[INFO] 'Status' property already has the required options.")
    except Exception:
        # Non-fatal: continue even if we cannot fetch/parse the schema
        logging.warning("Could not verify Status options after creation.")

    print(f"   URL: {new_db_response['url']}")
    return new_db_response

def handle_api_error(e: APIResponseError) -> None:
    """Logs and prints a user-friendly error message for Notion API errors."""
    logging.error(f"Notion API Error during production creation: {e}", exc_info=True)
    print(f"\n[ERROR] A Notion API error occurred: {e}")
    print("  - Please check that your API token has the correct permissions.")
    print("  - Ensure the parent page and master database IDs in your .env file are correct and shared with the integration.")
    print(f"  - Make sure a property named '{Config.PROD_MASTER_LINK_PROP}' of type 'URL' exists in your Productions Master DB.")

def print_final_instructions() -> None:
    """Prints the final instructions for the user after successful creation."""
    print("\n--- Automation Complete! ---")
    print("Next Step: You must now sync the application with your Notion changes.")
    print("Please run '[2] Sync Production Tables from Notion' from the main menu.")

def generate_next_production_id(db_id: str, title_prop: str) -> str:
    """Generates the next sequential ProductionID based on existing entries."""
    print("\n[INFO] Determining next ProductionID...")
    all_pages = nu.query_database(db_id)
    
    max_id = 0
    for page in all_pages:
        try:
            title_list = page.get("properties", {}).get(title_prop, {}).get("title", [])
            if title_list:
                prod_id_str = title_list[0].get("plain_text", "")
                if prod_id_str.startswith("PM") and prod_id_str[2:].isdigit():
                    current_id = int(prod_id_str[2:])
                    if current_id > max_id:
                        max_id = current_id
        except (ValueError, TypeError):
            continue # Ignore pages with malformed ProductionIDs
            
    next_id = max_id + 1
    new_prod_id = f"PM{next_id:03d}"
    print(f"  - Next ProductionID will be: {new_prod_id}")
    return new_prod_id

def add_to_master_list(production_name: str, db_url: str, productions_master_db_id: str, link_prop_name: str, title_prop_name: str, abbreviation: str, production_id: str) -> None:
    """
    Adds a new entry to the Productions Master database, linking to the new locations DB.
    """
    print(f"\n[INFO] Adding '{production_name}' to the Productions Master list...")
    
    master_page_properties = {
        title_prop_name: {"title": [{"text": {"content": production_id}}]},
        "Name": {"rich_text": [{"text": {"content": production_name}}]},
        link_prop_name: {"url": db_url},
        "Abbreviation": {"rich_text": [{"text": {"content": abbreviation}}]}
    }

    nu.create_page(productions_master_db_id, master_page_properties)
    logging.info(f"Successfully added '{production_name}' to Productions Master DB.")
    print("[INFO] Successfully added entry to Productions Master list.")

def main() -> None:
    """
    Guides the user through creating a new production and its associated
    Notion database, then adds it to the Productions Master list.
    """
    print("--- Create New Production Utility ---")
    
    # 1. Validate configuration from config.py
    required_vars = [
        'NOTION_TOKEN', 'PRODUCTIONS_MASTER_DB', 
        'LOCATIONS_MASTER_DB', 'NOTION_DATABASES_PARENT_PAGE_ID',
        'PROD_MASTER_TITLE_PROP'
    ]
    if not validate_config(required_vars):
        return

    production_name = input("Enter the name for the new production (e.g., 'Project Phoenix'): ").strip()
    if not production_name:
        print("[WARN] Production name cannot be empty. Aborting.")
        return

    while True:
        abbreviation = input("Enter a short, unique abbreviation for this production (e.g., 'PHX'): ").strip().upper()
        if abbreviation:
            break
        print("[WARN] Abbreviation cannot be empty.")

    try:
        # Generate the next ProductionID before creating any databases
        next_prod_id = generate_next_production_id(
            Config.PRODUCTIONS_MASTER_DB,
            Config.PROD_MASTER_TITLE_PROP
        )

        # Create the new database and get its URL
        new_db = create_locations_database(
            production_name,
            abbreviation,
            Config.NOTION_DATABASES_PARENT_PAGE_ID,
            Config.LOCATIONS_MASTER_DB
        )

        # Add the new production to the master list
        add_to_master_list(
            production_name,
            new_db["url"],
            Config.PRODUCTIONS_MASTER_DB,
            Config.PROD_MASTER_LINK_PROP,
            Config.PROD_MASTER_TITLE_PROP,
            abbreviation,
            next_prod_id
        )

        print_final_instructions()

    except APIResponseError as e:
        handle_api_error(e)

if __name__ == "__main__":
    # This block is for standalone execution, which is not the primary use case
    # but it's good practice to ensure it works.
    # The main application entry point (run.py) handles this setup.
    from dotenv import load_dotenv
    # Add project root ('c:\\Utils\\LocationsSync') to path to allow for clean imports
    project_root = Path(__file__).resolve().parents[1]
    sys.path.append(str(project_root))

    # Load environment variables for all scripts that will be called
    dotenv_path = project_root / '.env'
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path)
    else:
        print("Warning: .env file not found. Script may fail if config is not set in environment.")

    # Import and set up the central config after loading the .env file
    Config.setup()
    main()
