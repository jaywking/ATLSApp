# Standard library imports
import os
import json
import time
import sys
from pathlib import Path

# Third-party imports
from dotenv import load_dotenv

# Local application imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from scripts import notion_utils as nu


def _collect_db_ids_from_config() -> dict[str, str]:
    """Dynamically collects database IDs from the Config class."""
    db_sources = {}
    for attr in dir(Config):
        # Find attributes that are likely DB IDs (e.g., end with _DB or _DB_ID)
        if (attr.endswith("_DB") or attr.endswith("_DB_ID")) and not attr.startswith("_"):
            db_id = getattr(Config, attr)
            # Basic validation for a Notion ID
            if db_id and isinstance(db_id, str) and len(db_id.replace('-', '')) == 32:
                db_sources[db_id] = f"from .env variable '{attr}'"
    return db_sources

def _collect_db_ids_from_json(filepath: Path) -> dict[str, str]:
    """Collects database IDs from the notion_tables.json file."""
    db_sources = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            prod_dbs = json.load(f)
        for name, db_id in prod_dbs.items():
            if isinstance(db_id, str) and len(db_id.replace('-', '')) == 32:
                db_sources[db_id] = f"from {filepath.name} as '{name}'"
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"⚠️  Could not read '{filepath.name}'. Skipping databases from this source.")
    return db_sources

def _generate_report_for_db(db_id: str, source: str) -> list[str]:
    """Fetches a single database's schema and formats it for the report."""
    print(f"  - Inspecting {db_id}...")
    try:
        data = nu.get_database(db_id)
        db_title = data.get('title', [{}])[0].get('plain_text', 'Untitled')
        lines = [f"Database: '{db_title}'", f"ID: {db_id}", f"Source: {source}", "-"*30]
        properties = sorted(data.get("properties", {}).items())
        for name, meta in properties:
            lines.append(f"  - {name} (Type: {meta['type']})")
        lines.extend(["", "="*50, ""])
        return lines
    except Exception as e:
        return [f"Database: UNKNOWN", f"ID: {db_id}", f"Source: {source}", f"  - ❌ FAILED TO FETCH SCHEMA: {e}", "", "="*50, ""]

def main() -> None:
    """
    Inspects all databases configured in .env and notion_tables.json
    and writes the output to schema_report.txt.
    """
    print("🔎 Generating a full schema report for all configured databases...")

    # --- Collect all known database IDs ---
    # 1. From .env file (via Config)
    db_sources = _collect_db_ids_from_config()

    # 2. From notion_tables.json, merging with the results from .env
    json_dbs = _collect_db_ids_from_json(project_root / "notion_tables.json")
    for db_id, source in json_dbs.items():
        db_sources.setdefault(db_id, source) # Add if not already present
    
    if not db_sources:
        print("❌ No database IDs found in .env or notion_tables.json. Cannot generate report.")
        return

    # --- Generate the report ---
    report_lines = [
        "Notion Database Schema Report",
        f"Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "="*50, ""
    ]

    for db_id, source in sorted(db_sources.items()):
        report_lines.extend(_generate_report_for_db(db_id, source))

    # --- Write to file ---
    output_path = project_root / "schema_report.txt"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"\n✅ Report successfully generated: {output_path}")
    print("You can now copy the contents of this file and share it.")

if __name__ == "__main__":
    # Load .env from the project root to make the script runnable from anywhere
    load_dotenv(dotenv_path=project_root / '.env')
    main()