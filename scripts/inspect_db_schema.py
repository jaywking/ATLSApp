# Standard library imports
import os
import sys
from pathlib import Path
import argparse
import json

# Set up project root and load environment variables FIRST.
# This is crucial so that modules like 'config' have access to them when imported.
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

# Third-party imports
from dotenv import load_dotenv
load_dotenv(dotenv_path=project_root / '.env')

# Local application imports
from scripts import notion_utils as nu
from config import Config

NOTION_TABLES_PATH = project_root / "notion_tables.json"
_EXIT_SENTINEL = "__EXIT__"


def _load_known_tables() -> dict[str, str]:
    if not NOTION_TABLES_PATH.is_file():
        return {}
    try:
        data = json.loads(NOTION_TABLES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _prompt_database_selection() -> str | None:
    table_map = _load_known_tables()
    if not table_map:
        return None

    entries = list(table_map.items())
    print("Available databases from notion_tables.json:")
    for idx, (label, db_id) in enumerate(entries, start=1):
        preview = (db_id[:8] + "…") if isinstance(db_id, str) and len(db_id) > 8 else db_id
        print(f"[{idx}] {label} ({preview})")
    print("[M] Manual entry")
    print("[Q] Return to main menu")

    while True:
        choice = input("> ").strip().lower()
        if not choice or choice == "q":
            return _EXIT_SENTINEL
        if choice == "m":
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(entries):
                return entries[idx - 1][1]
        print("Invalid selection. Choose a number, 'M', or 'Q'.")

def main(db_id_or_url: str | None = None) -> None:
    """
    Prompts the user for a Notion Database ID or URL and prints its schema.
    Can also accept the ID/URL as an argument.
    """
    if not db_id_or_url:
        selected = _prompt_database_selection()
        if selected == _EXIT_SENTINEL:
            print("Returning to main menu.")
            return
        if selected:
            db_id_or_url = selected
        else:
            db_id_or_url = input("Enter the Notion Database ID or URL to inspect (blank to cancel): ").strip()
            if not db_id_or_url:
                print("No database selected. Returning to main menu.")
                return

    # Sanitize the input to get a clean database ID
    db_id = db_id_or_url.split('?')[0].split('/')[-1]

    try:
        data = nu.get_database(db_id)
        title = data.get('title', [{}])[0].get('plain_text', db_id)
        print(f"\nDatabase '{title}' (ID: {db_id}) properties:\n")

        for name, meta in sorted(data["properties"].items()):
            prop_type = meta['type']
            details = ""
            # For select, multi_select, and status, show the available options
            if prop_type in ("select", "multi_select", "status"):
                options = meta.get(prop_type, {}).get("options", [])
                if options:
                    option_names = [f"'{opt['name']}'" for opt in options]
                    details = f" (Options: {', '.join(option_names)})"
            print(f"  - {name} (Type: {prop_type}){details}")
    except Exception as e:
        print(f"\n[ERROR] Could not inspect database: {e}")

if __name__ == "__main__":
    # Initialize Config from environment (after load_dotenv above)
    Config.setup()
    # Check for the Notion token before proceeding
    if not Config.NOTION_TOKEN:
        print("? NOTION_TOKEN not found in your .env file or environment variables.")
        print("   Please ensure a valid token is set in the .env file at the project root.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Inspect the schema of a Notion database."
    )
    parser.add_argument(
        "db_id_or_url",
        nargs="?",
        default=None,
        help="The ID or URL of the Notion database to inspect (optional)."
    )
    args = parser.parse_args()

    main(args.db_id_or_url)
