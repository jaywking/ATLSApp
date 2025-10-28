Scripts — Agent Guidance

Scope
- This file applies to all code under `scripts/`.

Conventions
- Entry function: expose a top-level `run()` (or `main()` if it’s a standalone CLI). Keep user prompts concise.
- Imports: prefer `from config import Config` and `from scripts import notion_utils as nu` for Notion calls.
- Env: ensure `.env` is loaded by the caller; call `Config.setup()` before using config values.
- Notion API: use `nu.query_database`, `nu.get_page`, `nu.update_page`, etc. Do not call `requests` directly.
- Paths: use `pathlib.Path` and project root via `Path(__file__).resolve().parents[1]` when needed.
- Logging: CSV logs in `logs/`, UTF-8-SIG encoding, write header if file doesn’t exist. Avoid noisy console output.
- Files: do not open GUIs; print status and full output paths.
- Templates: keep placeholders stable and documented in `AGENTS.md` (root) and topic handbooks in `docs/`.

Menu Integration
- To expose a script in the main menu:
  1) Import in `scripts/__init__.py` and export a callable.
  2) Add a menu item in `run.py` pointing to that callable.

