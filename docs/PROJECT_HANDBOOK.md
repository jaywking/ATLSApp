# Locations Sync Project Handbook
Last updated: 2025-10-26 22:30:00 UTC

This document captures how the Locations Sync toolkit is wired together so another developer (or AI assistant) can take over without reverse-engineering the codebase. It complements `README.md` by diving into the moving pieces, runtime workflow, and outstanding gaps.


## Agent Quickstart Notes

- **Default shell/runtime**: Our local automation and Codex CLI sessions run under PowerShell (`pwsh.exe`). When invoking shell commands (including helper scripts), prefer PowerShell-friendly syntax and flags; only switch to Bash when a script explicitly requires it.
- **Planning convention**: For multi-step requests (anything beyond trivial single-file edits), draft a short plan before touching code and keep it updated as you move through the steps. This mirrors how we work in the CLI and helps capture intent for future reviewers.
- **Diagnosing enrichment bugs**: Issues with medical facility enrichment usually live in `scripts/fetch_medical_facilities.py`, `scripts/notion_utils.py`, or `scripts/google_utils.py`. Start by checking how we build Notion payloads, relation properties, and the Google response fields we map (`formatted_address`, `address_components`, `types`, etc.).
- **Post-processing refresh**: Options 3/4 automatically trigger a targeted medical facility refresh for the updated master locations; no manual step needed after geocoding.
- **Facility cleanup helper**: `scripts/cleanup_orphan_facilities.py` runs the backfill routine to remove orphaned facility links after deletions.
- **Google Places field expectations**: We rely on `formatted_address` plus `address_components` to normalize US addresses into the format `123 Main St, City, ST 12345, USA`. If a facility record is missing state, ZIP, or country, revisit how we decompose `address_components` and rebuild the full address string.

---

## 1. High-Level Overview

- **Goal**: Keep multiple Notion databases in sync for TV/film production locations. A "production" gets its own locations database, all feeds into a shared "Locations Master" table, and optional enrichment data (medical facilities).
- **Primary automation**: `run.py` orchestrates everything through a CLI menu. Scripts live in `scripts/`; shared infrastructure lives in `config.py`, `scripts/notion_utils.py`, and `scripts/google_utils.py`.
- **Web control surface**: `app/main.py` wraps the core CLI scripts with FastAPI + NiceGUI pages. Each endpoint shells out to the existing scripts asynchronously so the UI stays responsive while reusing proven logic.
- **External dependencies**: Notion API plus Google Maps APIs (Geocoding + Places Details + Nearby).
- **Persistent storage**: Only Notion databases—no relational database at the moment. `scripts/PostgreSQLdb.py` exists but is unused.

```
Notion (Productions Master) ──▶ Sync script ──▶ notion_tables.json
            │                           │
            │                           ├──▶ Process new locations (per production)
            ▼                           │
Notion (Production-specific DB)         │
            │                           └──▶ Re-process (full refresh)
            ▼
Notion (Locations Master) ──▶ Medical enrichment, reports, etc.
```

---

## 2. Data Model & Key Notion Databases

| Database | Description | Critical Properties |
|----------|-------------|----------------------|
| **Productions Master** (`Config.PRODUCTIONS_MASTER_DB`) | Lists each production and the URL to its locations database. | `ProductionID` (title), `Locations Table` (URL relation), `Abbreviation` (rich text). |
| **Production Locations** (one per production) | Operates per production. Populated/updated by `process_new_locations`. | `ProductionID` (relation), `ProdLocID` (title), `LocationsMasterID` (relation), `Abbreviation` (rollup), `Location Name`, `Practical Name`, `Status`, `Full Address`, `Google Maps URL`, `Place_ID`, `Latitude/Longitude`, `Created time`, `Last edited time`.
| **Locations Master** (`Config.LOCATIONS_MASTER_DB`) | Global table of all unique locations. | `LocationsMasterID` (title), `Full Address`, `Latitude/Longitude`, `Place_ID`, `Practical Name`, `Name`, `Types`, `Google Maps URL`, `Vicinity`.
| **Medical Facilities** (`Config.MEDICAL_FACILITIES_DB`) | Nearby urgent care / ER places. | `FacilitiesMasterID` (generated), relation back to `Locations Master`, facility metadata.

**Status handling**
- Because Notion does not let the API manage status options, the scripts assume the production databases already have the options named `Ready`, `Matched`, and `Error` (or the values set in `Config.STATUS_*`). `sync_prod_tables` can detect missing options but still requires manual fixes unless run with `--autofix-status`.

---

## 3. Configuration & Environment

1. Python 3.9+ recommended.
2. Install dependencies: `pip install -r requirements.txt` (primarily `requests`, `python-dotenv`, `tqdm`).
3. Create `.env` in project root:

   ```ini
   NOTION_TOKEN=...
   GOOGLE_MAPS_API_KEY=...
   PRODUCTIONS_DB_ID=...
   PRODUCTIONS_MASTER_DB=...
   LOCATIONS_MASTER_DB=...
   MEDICAL_FACILITIES_DB=...
   NOTION_DATABASES_PARENT_PAGE_ID=...
   # Optional overrides
   STATUS_ON_RESET=Ready
   STATUS_AFTER_MATCHING=Matched
   STATUS_ERROR=Error
   ```

4. `run.py` loads `.env`, calls `Config.setup()`, and every helper module pulls credentials from `Config` at runtime. Always launch scripts through `run.py` or ensure `Config.setup()` is called manually.

---

## 4. CLI Menu & Recommended Workflow

Launch the CLI via `python run.py`. The menu loops until a valid input (`1`-`9`, `R`, `Q`) is provided, so hitting Enter does nothing.

1. **Create New Production** (`scripts/create_new_production.py`)
   - Collects production name + abbreviation.
   - Creates a new production-specific Notion database named `<ABBREVIATION>_Locations` under `NOTION_DATABASES_PARENT_PAGE_ID`, pre-populates the description with the Notion database ID, and applies the canonical schema.
   - Inserts a row into the Productions Master DB with the new ProductionID, name, URL, abbreviation.
   - Notion still ignores API attempts to seed Status options—open the new database in Notion and add a `Status` property with options `Ready`, `Matched`, `Error` (set `Ready` as the default).

2. **Sync Production Tables from Notion** (`scripts/sync_prod_tables.py`)
   - Pulls the master list of productions, writes `notion_tables.json`, and validates/auto-fixes schema drift.
  - Optional flags: `--autofix-status` (add missing Status options where allowed) and `--autofix-schema` (fix common property names/types).

3. **Process New Production Locations** (`scripts/process_new_locations.py::run(process_all=False)`)
   - Prompts which production DB to target.
   - Validates schema against `get_locations_db_schema`.
   - Auto-links the `ProductionID` relation on rows missing it (by looking up the production in the master table).
   - Fetches all rows but filters down to those with Status == `Ready` by default.
   - Normalises the address using Google’s formatted result and offers `--log-mode {auto,immediate,buffered,off}` to control CSV logging.
   - For each row:
     - Builds an address string (prefers `Full Address`, falls back to `Practical Name` if needed).
     - Geocodes via Google Maps (`scripts/google_utils.geocode`).
     - Assigns a new `ProdLocID` prefixing with the production abbreviation and preserving incremental numbering.
     - Either links to an existing master record (match by Place ID or Haversine distance) or creates a new master entry (with Google metadata).
     - Updates the production record with coordinates, `LocationsMasterID`, `ProdLocID`, `Status = Matched`, and logs to `logs/process_new_locations_log.csv`.

4. **Re-process Production Locations** (`scripts/process_new_locations.py::run(process_all=True)`)
   - Same as step 3 but processes every row, regardless of status.
   - When a row already has a master match, the script refreshes the master page with the latest Google metadata and coordinates.
   - Provides a safety prompt (“Are you sure?”) before continuing.

5. **Fetch Nearby Medical Facilities** (`scripts/fetch_medical_facilities.py`)
   - Iterates every Locations Master page, fills the configured slots (UC1–UC3, ER), creates/links facility records, and writes a CSV log in `logs/`.
   - This script expects master rows to have Latitude/Longitude already in place.

6. **Backfill Medical Facilities** (`scripts/fetch_medical_facilities.py::run_backfill`)
   - Repairs missing facility-to-master backlinks and rehydrates facility details (addresses, phone numbers, URLs, hours, types) using Google Place Details. Supports dry-run mode.

7. **Inspect a specific DB Schema** (`scripts/inspect_db_schema.py`)
   - Ad-hoc inspection tool for a single database ID or URL.

8. **Generate Full Schema Report** (`scripts/generate_schema_report.py`)
   - Walks all known databases and dumps their schema into `schema_report.txt`. Useful pre-flight check before env changes.

9. **Wipe Data Utility (DANGER)** (`scripts/wipe_utility.py`)
   - Resets production location rows, deletes master/medical data, etc. Intended only for test environments.

The menu also understands `R` (restart the script process) and `Q` (quit).

---

## 5. Module & Script Breakdown

### 5.1 Core Infrastructure

- `config.py`: Centralizes environment variables. Always call `Config.setup()` after loading `.env`.
- `scripts/notion_utils.py`: Thin wrapper over `requests` with retry logic and convenience methods for Notion CRUD operations plus formatting helpers.
- `scripts/google_utils.py`: Shared Google API helpers (`geocode`, `place_details`, `nearby_places`) with backoff logic.

### 5.2 Production Lifecycle

- `scripts/create_new_production.py`
  - Schema generator `get_locations_db_schema()` defines required properties (`ProdLocID`, `Practical Name`, `Location Name`, `Status`, relations, rollups, coordinates, etc.).
  - `create_locations_database()` creates a `<ABBREVIATION>_Locations` database, stores the production name + database ID in the description, and remediates the Status property when possible.
  - Also writes a new entry to the Productions Master list linking to the new database.

- `scripts/sync_prod_tables.py`
  - Reads `Config.PRODUCTIONS_MASTER_DB`, writes `notion_tables.json` (used everywhere for selection lists).
  - `validate_database_schema()` compares each production DB against `REQUIRED_SCHEMA` and can rename / change property types if needed.
  - `ensure_status_options()` warns if required status options are missing.

- `scripts/process_new_locations.py`
  - Contains the heavy lifting for per-row processing.
  - Auto-deduces production abbreviation (`production_prefix`) so `ProdLocID`s stay sequential even if previous values exist.
  - Fallback to full address for geocoding to avoid ambiguous practical names.
  - Keeps an in-memory dictionary of master entries for fast matching.
  - Supports dry-run mode when invoked manually (`python scripts/process_new_locations.py --dry-run`).
  - New helper `_build_master_properties()` standardizes the payload used both for creating and refreshing master entries.

- `scripts/fetch_medical_facilities.py`
 - Uses Google Nearby Search to populate/refresh urgent care and ER relations for each master location.
 - Maintains CSV logs in the `logs/` folder for auditing.

- **Backfill Medical Facilities** (`scripts/fetch_medical_facilities.py::run_backfill` via menu)
   - Repairs missing facility-to-master backlinks (Facilities DB relation pointing to Locations Master).
   - Enriches facility rows with missing details from Google Place Details when `Place_ID` exists: Address, Website, Google Maps URL, Phone (prefers Google's formatted number and falls back to international), Type, and per-day opening hours. Detects and rewrites truncated addresses automatically.
   - Prompts for dry-run mode before execution. In dry-run, it prints intended updates without modifying Notion.

### 5.3 Reporting & Utilities

- `scripts/generate_schema_report.py`: Walks configured databases, dumps property details.
- `scripts/inspect_db_schema.py`: Interactive schema dump for a single DB.
- `schema_report.txt`: Generated artefact consumed manually.
- `scripts/prune_logs.py`: CLI helper to prune `logs/` and root artefacts (`.csv`, `.log`, `.zip`) older than a retention window (default 30 days). Supports `--dry-run`, `--days`, and custom paths/extensions.
- `scripts/normalize_facility_hours.py`: Maintenance helper to standardise existing medical facility hour fields by stripping duplicated weekday prefixes. Supports `--dry-run`.

### 5.4 Legacy / Supporting Scripts

- `scripts/match_location_master.py`: Older utility partially superseded by `process_new_locations`. Keep for reference; avoid running in production.
- `scripts/PostgreSQLdb.py`: Stub for a database integration that is not currently wired in.
- `check_all_dbs.py`, `check_token.py`, `insert_row_test.py`, `test_productions_master.py`: Local testing helpers.

Document any additional one-off scripts you introduce so future maintainers understand the provenance.

---

## 6. Files, Logs & Version Control

- **logs/**: Processing scripts append CSV logs here. Run `python -m scripts.prune_logs --dry-run` to preview removals, then rerun without `--dry-run` to delete files older than 30 days (default). Adjust the retention window with `--days N`.
- **notion_tables.json**: Generated map of production display name -> database ID. Regenerated whenever “Sync Production Tables” runs.
- `.gitignore` excludes environment files, caches, logs, archives, and editor junk (e.g., `.env`, `__pycache__/`, `logs/`, `*.pyc`, `*.zip`, `.DS_Store`).

---

## 7. Known Gaps & Manual Steps

- **Status property setup**: New databases still require manual addition of the Status options (`Ready`, `Matched`, `Error`). `sync_prod_tables` can warn but not automatically fix due to API limitations.
- **Geocoding failures**: `process_new_locations` skips rows when Google cannot geocode an address (or when API key is missing). The CSV log includes failed addresses.
- **Medical facilities enrichment**: Currently only runs when explicitly invoked. There is no scheduler in this repo; the README mentions a GitHub Action, but the workflow file is not present.
- **Legacy scripts**: Several older scripts remain in the repo for historical reasons. Confirm before using them—they may expect different schemas.
- **.gitignore**: Already configured to ignore common artefacts and logs.
- **Error handling**: Reprocessing now diff-checks master metadata and skips redundant updates. Manual curation still recommended for critical pages.
- **PostgreSQL integration**: Placeholder module exists but is unused; either delete or wire it up if needed.

---

## 8. Onboarding Checklist for New Maintainers

1. Clone the repo and create your `.env` from the template above.
2. Run `pip install -r requirements.txt`.
3. Launch `python run.py` and verify the menu appears.
4. Choose option **2** (Sync Production Tables) to generate `notion_tables.json`.
5. Use option **3** to process any new locations. Check `logs/process_new_locations_log.csv` for results.
6. If you need a complete refresh, run option **4**—confirm the prompt carefully.
7. Optionally run **5** to fetch nearby medical facilities for fresh master locations.
8. Optionally run **6** to backfill facility details and repair backlinks if you suspect gaps.
9. Review `logs/` and Notion tables to confirm data updates succeeded.
10. When creating a new production, remember to configure the `Status` property and add the options (`Ready`, `Matched`, `Error`) manually in Notion afterward.

Keep this handbook updated when you make structural changes so future hand-offs stay painless.


---
**Backfill Internals & Troubleshooting**
- Performance: Deduplicates facilities, bulk-loads facility properties, parallelizes Google Place Details, and applies Notion updates with small concurrency.
- Safe updates: Merges backlink repair and enrichment into a single PATCH per page.
- Dry-run: Menu option 6 prompts; dry-run prints intended updates without writing.
- Warnings: Occasional Notion 'Read timed out' warnings are retried automatically.
- Tuning (optional): Increase Notion timeout in scripts/notion_utils.py, or lower update concurrency in scripts/fetch_medical_facilities.py if your workspace is under load.

---

## 9. Web UI (Local)

### FastAPI + NiceGUI control surface
- Location: `app/main.py`. Exposes a FastAPI backend and NiceGUI front-end with sidebar navigation.
- Run: `uvicorn app.main:app --reload`
- Actions available:
  - Process Locations (`scripts/process_new_locations.py`)
  - Reprocess Locations (`scripts/process_new_locations.py --process-all`)
  - Fetch Nearby Medical Facilities (`scripts/fetch_medical_facilities.py`)
  - Backfill Facilities (`scripts/fetch_medical_facilities.py --backfill`)
  - Generate LHA (`scripts/generate_lha_forms.py`)
- Each action triggers the existing CLI scripts asynchronously and streams summaries into the page log.
- Logging: `logs/jobs.log` captures script name, arguments, return code, duration, and truncated stdout/stderr for every run. The NiceGUI pages also keep a per-session log panel for quick troubleshooting.

### Legacy Streamlit UI
- Location: `ui/app.py` (Streamlit). Optional password via `.env` `LOCATIONSYNC_PASSWORD` (or `APP_PASSWORD`).
- Install: `pip install -r requirements.txt` then `pip install -r requirements-ui.txt`
- Run: `streamlit run ui/app.py`
- Actions mirror the CLI menu; verify `notion_tables.json` exists (run Sync first) for processing flows.

---

## 10. Quick Resume (Post-Interruption)

- Validate environment
  - `pip install -r requirements.txt`
  - `pip install -r requirements-ui.txt`
  - Ensure `.env` has: `NOTION_TOKEN`, `GOOGLE_MAPS_API_KEY`, `PRODUCTIONS_DB_ID`, `LOCATIONS_MASTER_DB`, `PRODUCTIONS_MASTER_DB`, `MEDICAL_FACILITIES_DB`
- CLI quick test
  - `python run.py` -> choose [2] Sync, then [3]/[4]/[5]/[6] as needed
- UI quick test
  - `uvicorn app.main:app --reload` -> open `http://127.0.0.1:8000`
  - Run "Process Locations" (or other actions) and confirm status/log output; check `logs/jobs.log` for matching entries
  - Legacy: `streamlit run ui/app.py` remains available if you prefer the older control surface
- Common fixes
  - Missing `notion_tables.json` -> run Sync
  - Status options missing -> rerun Sync with `--autofix-status` or add manually in Notion
  - Google failures -> check `GOOGLE_MAPS_API_KEY` and quotas

---

## 11. Next Steps (Open TODOs)

- UI: Add pre-run validation for required env vars and DB IDs; surface friendlier error messages.
- UI: Add progress bars per step (SSE/polling) beyond raw stdout capture.
- Caching: Consider SQLite (local) for Google geocode/place_details cache to reduce cost/latency.
- Jobs/Audit: Optional DB (SQLite/Postgres) for job history, retries, and exactly-once semantics.
- Deployment: If multi-user later, graduate to FastAPI backend + HTMX/React.

---

## 12. Troubleshooting Cheatsheet

- “Read timed out” from Notion
  - Retries are built in; rerun. Reduce concurrency in `scripts/fetch_medical_facilities.py` if needed.
- Processing shows 0 items
  - Ensure Status is set to `Ready` (or `Config.STATUS_ON_RESET`) and `Place_ID` is empty for new rows; or toggle “Process all rows”.
- Re-process doesn’t update master
  - Confirm `Config.LOCATIONS_MASTER_DB` is correct and network/API permissions are valid.
- UI shows no productions
  - Run “Sync Production Tables” to regenerate `notion_tables.json`.

---

## 13. Decision Log (Why Things Are This Way)

- No DB by default: Simplicity; Notion is source of truth. Add SQLite/Postgres when caching/jobs/audit become valuable.
- Streamlit for UI: Fastest local GUI; can migrate to FastAPI for multi-user/auth/background jobs later.
- Status model: Controlled via Notion Status options; script supports `--autofix-status` to reduce manual steps.


