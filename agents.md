# ATLSApp – Agent Quickstart

**Purpose:**  
ATLSApp is the browser-based version of the LocationsSync toolkit.  
It wraps existing CLI scripts (Notion + Google Maps automations) with a FastAPI + NiceGUI web interface.

---

## Repository Structure
/app
main.py ← FastAPI + NiceGUI entry point
/services ← adapters that call CLI scripts
/scripts
(existing LocationsSync scripts)
process_new_locations.py
fetch_medical_facilities.py
generate_lha_forms.py
/docs
PROJECT_HANDBOOK.md
LHA_GENERATION.md
HANDOFF.md
AGENTS.md
.env
requirements.txt

---

## Environment

- **Python 3.9+**
- `.env` file with:
NOTION_TOKEN=
GOOGLE_MAPS_API_KEY=
PRODUCTIONS_DB_ID=
LOCATIONS_MASTER_DB=
PRODUCTIONS_MASTER_DB=
MEDICAL_FACILITIES_DB=

- Run locally with:
```sh
uvicorn app.main:app --reload
