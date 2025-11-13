import json
import os
from pathlib import Path
from typing import List

from config import Config

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_TABLES_PATH = _ROOT_DIR / 'notion_tables.json'


def run_preflight(check_tables: bool = True) -> List[str]:
    """Return a list of user-facing issues blocking script execution."""
    Config.setup(force=True)

    issues: List[str] = []

    if not Config.NOTION_TOKEN or not os.getenv('NOTION_TOKEN'):
        issues.append('Missing NOTION_TOKEN in .env.')
    if not Config.GOOGLE_MAPS_API_KEY or not os.getenv('GOOGLE_MAPS_API_KEY'):
        issues.append('Missing GOOGLE_MAPS_API_KEY in .env.')

    if not Config.PRODUCTIONS_DB_ID:
        issues.append('Missing PRODUCTIONS_DB_ID in .env.')
    if not Config.LOCATIONS_MASTER_DB:
        issues.append('Missing LOCATIONS_MASTER_DB in .env.')
    if not Config.MEDICAL_FACILITIES_DB:
        issues.append('Missing MEDICAL_FACILITIES_DB in .env.')

    if check_tables:
        try:
            with _TABLES_PATH.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not data:
                issues.append('notion_tables.json is empty; run sync_prod_tables.')
        except FileNotFoundError:
            issues.append('notion_tables.json is missing; run sync_prod_tables.')
        except json.JSONDecodeError:
            issues.append('notion_tables.json is malformed; re-run sync_prod_tables.')

    return issues
