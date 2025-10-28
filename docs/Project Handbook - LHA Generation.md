# Overview
*Last updated: 2025-10-26 22:08:28 -04:00*

This handbook documents how the *Generate LHA Forms* workflow currently behaves inside the **Locations Sync** project. It captures the script entry points, template context, data sourcing, and logging so the real production template and future automation can build on the existing proof-of-concept.

The end-user experience today:

1. Launch `run.py` and choose menu option **[10] Generate LHA Forms**.
2. Pick a production from `notion_tables.json`.
3. Pick a location fetched live from Notion.
4. The script renders `templates/LHA_Template.docx` with current data and writes a DOCX to the chosen folder.
5. An audit entry is appended to `logs/lha_generation_log.csv`.

---

## Current Implementation Snapshot

### Script Workflow (`scripts/generate_lha_forms.py`)

- `run()` is the single public entry point and is imported in `scripts/__init__.py` for the main menu.
- Execution steps (loop continues until the operator presses Enter at the location prompt):
  1. Load environment variables (`dotenv` + `Config.setup()`).
  2. Load the production → database map from `notion_tables.json`.
  3. Query the selected production database via `scripts/notion_utils.query_database` and prompt for a location.
  4. Fetch location details, the linked master page, and facility pages (UC1, UC2, ER) using `notion_utils.get_page`.
  5. Shape the context: normalize addresses, group hours into contiguous day ranges with newline-separated output (e.g., `Monday-Friday: 8:00 AM - 8:00 PM` on one line, `Saturday-Sunday: Closed` on the next), attach raw URLs, and build `docxtpl.RichText` hyperlinks for Google Maps and website entries.
  6. Render `templates/LHA_Template.docx` with `docxtpl` and save the DOCX to the requested directory.
  7. Append a CSV row to `logs/lha_generation_log.csv`.
- Error handling prints actionable messages for missing configuration, missing template, or missing `docxtpl`.

### Template & Context Fields

- The active template is `templates/LHA_Template.docx` and uses Jinja2 placeholders.
- All keys supplied to the template are listed in `docs/lha_template_fields.txt`. Common ones include:
  - `production_name`, `production_abbrev`, `location_name`, `practical_name`, `full_address`, `latitude`, `longitude`.
  - Facility groups: `uc1_*`, `uc2_*`, `er_*` for names, addresses, phones, grouped hours (multi-line), websites, and map links.
  - Hyperlink-ready keys: `google_maps_link`, `uc1_website_link`, `uc1_maps_link`, etc. These are `RichText` objects and must use the `|safe` filter in the template (e.g., `{{ google_maps_link|safe }}`).
- New placeholders can be introduced by expanding the context in `_render_and_save()`; undefined placeholders safely render as empty strings.

### Output Path & Logging

- Output directories are remembered per production abbreviation inside `lha_paths.json`. The first run prompts for a path; subsequent runs show the stored path with a reuse-or-edit prompt. Paths are stored as UTF-8 BOM JSON in the project root.
- Files are saved as `<ProductionAbbrev>_<LocationName>_LHA.docx`, with unsafe filename characters replaced by underscores.
- Log file: `logs/lha_generation_log.csv` (UTF-8-SIG) with columns:
  - `timestamp`, `production_name`, `production_abbrev`, `location_page_id`, `location_name`, `practical_name`, `output_file`.

### Notion Data Sources

- **Production Locations DB** (ID pulled from `notion_tables.json`): supplies location/practical names, address, lat/lng, production rollups, and the relation to the Locations Master entry.
- **Locations Master DB**: provides canonical address, Google Maps URL, and relations for facility slots (`UC1`, `UC2`, `ER`).
- **Medical Facilities DB**: stores facility details (names, addresses, phone numbers, websites, Google Maps URLs, and weekday-specific hours) that feed UC/ER sections.

> For quick-start context, environment setup, and schema references, see the repository-level `AGENTS.md` plus the scripts-specific `scripts/AGENTS.md`.

---

## Future Enhancements

- Persist per-production output paths (`lha_paths.json`) so the user is prompted only once.
- Batch-generate LHAs for multiple locations in a production.
- Optional PDF conversion workflow and/or automatic upload back into Notion.
- Capture operator notes (hazards, mitigations) prior to rendering.
- Support additional facility slots if Notion adds more relations (e.g., UC3).
