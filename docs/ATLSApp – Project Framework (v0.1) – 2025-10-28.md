**Updated:** 2025-10-28

---

## 1. Overview
**Tracking:** ChatGPT Project (this thread)  
**Development:** VS Code with Codex 5  
**Local path:** `C:\utils\ATLSApp`  

ATLSApp is the browser-based evolution of your LocationsSync toolkit.  
It uses **NiceGUI + FastAPI** for the user interface and backend while re-using your proven CLI scripts for location processing, medical facility enrichment, and LHA generation.

---

## 2. Core Design
| Layer | Purpose | Technology |
|-------|----------|-------------|
| **UI** | Web GUI for interacting with all tools (maps, PDFs, DOCX, photos, job logs) | NiceGUI |
| **API** | REST endpoints that wrap your existing scripts | FastAPI |
| **Services** | Thin adapters calling the working Python scripts | Python modules |
| **Jobs** | Background task queue for long operations | RQ / Celery / Arq (TBD) |
| **Storage** | Handles PDF/DOCX/image storage & preview | S3 / R2 / local disk |
| **Config** | Loads `.env` variables and manages tokens | `pydantic.BaseSettings` |

---

## 3. Folder Structure
```
C:\utils\ATLSApp
├─ app\
│  ├─ main.py
│  ├─ ui\                  # NiceGUI pages
│  ├─ api\                 # FastAPI routers
│  ├─ services\            # Adapters for existing scripts
│  ├─ workers\             # Background jobs (optional)
│  ├─ static\              # pdf.js, icons, CSS
│  ├─ templates\           # HTML snippets if needed
│  └─ config.py
├─ scripts\                # Existing CLI scripts reused as modules
│  ├─ process_new_locations.py
│  ├─ fetch_medical_facilities.py
│  ├─ generate_lha_forms.py
│  └─ (others as needed)
├─ tests\
├─ .env.sample
├─ requirements.txt
├─ requirements-dev.txt
├─ README.md
└─ Makefile
```

---

## 4. Integration with Existing Scripts
- Your current CLI scripts remain intact under `scripts\`.
- New **service adapters** in `app/services/` import and call them directly.
  - `locations_service.py` → calls `scripts.process_new_locations.run()`
  - `facilities_service.py` → calls `scripts.fetch_medical_facilities.run()`
  - `lha_service.py` → calls `scripts.generate_lha_forms.run()`
- The FastAPI layer provides REST endpoints to trigger these services.
- NiceGUI buttons call the endpoints and display progress or logs.

**Result:** The GUI becomes a control surface for your proven automation rather than a rewrite.

---

## 5. Initial Milestones
**M1 – Scaffold**
- Create the folder structure and stub files.
- Verify local run (`uvicorn app.main:fastapi_app --reload`).

**M2 – Wire Existing Scripts**
- Build adapters for location and facility processing.
- Add API endpoints and a simple NiceGUI “Process Locations” button.

**M3 – Files & Previews**
- Add PDF preview (using pdf.js).
- DOCX download or conversion to PDF for preview.
- Stub “Jobs” page.

**M4 – Google Maps Integration**
- Embed Google Maps JS API key.
- Pin one test location to verify integration.

---

## 6. Project Tracking
All progress will be tracked here in this ChatGPT project.  
Each major update (framework changes, milestones, design decisions) will receive its own dated note.  

Inside the repo, maintain:
- `README.md` – setup, run, and contribution notes  
- `docs/roadmap.md` – feature backlog and timeline  
- `docs/decisions.md` – concise records of major technical choices  

---

## 7. Configuration & Secrets
`.env` file (not committed to Git):
```
NOTION_TOKEN=
GOOGLE_MAPS_API_KEY=
S3_BUCKET=
S3_ENDPOINT=
APP_PASSWORD=
```
`app/config.py` loads these values via Pydantic Settings.

---

## 8. Repo & File Visibility
GitHub and ChatGPT integration will be used for code review once access is confirmed.  
Until live repo browsing is available, files can be shared here via upload or raw GitHub links for analysis.

---

## 9. Next Steps
1. Finalize this scaffold in `C:\utils\ATLSApp`.  
2. Import your working scripts into the `scripts\` folder.  
3. Create service adapters (`app/services/`) for at least one script.  
4. Verify a local run of the app showing the sidebar layout and a working “Process” button.  
5. Document the results and push the repo to GitHub for ongoing iteration.

---

**Prepared:** 2025-10-28  
**Author:** Above the Line Safety – ATLSApp Framework
