# scripts/__init__.py

"""
This file makes the 'scripts' directory a Python package and can define
the package's public API.
"""

from . import notion_utils
from . import google_utils
from .create_new_production import main as create_new_production
from .process_new_locations import run as process_new_locations, run_reprocess as reprocess_new_locations
from .inspect_db_schema import main as inspect_db_schema
from .sync_prod_tables import main as sync_prod_tables # Assuming this filename is correct
from .wipe_utility import main as wipe_prod_locations # Corrected filename
from .fetch_medical_facilities import main as fetch_medical_facilities
from .generate_schema_report import main as generate_schema_report
from .fetch_medical_facilities import run_backfill as backfill_medical_facilities, run_facility_refresh
from .generate_lha_forms import run as generate_lha_forms
