"""
Proof-of-concept: Generate an LHA form (DOCX) for a selected production/location
using live data from Notion and a Word template rendered via docxtpl.

Steps:
1) Prompt user to select a production (from notion_tables.json)
2) Fetch and list all locations for that production (from Notion)
3) Prompt user to select one location
4) Fetch location details and related facility info (UC1/UC2/ER) from Notion
5) Render templates/LHA_Template.docx using docxtpl
6) Save as <ProductionAbbrev>_<LocationName>_LHA.docx in a user-specified folder
7) Append an entry to logs/lha_generation_log.csv

Note:
- Uses Config.setup() to load env vars
- Uses scripts/notion_utils.py for Notion HTTP calls
- Keeps all logic within this file and exposes a single run() function
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party
try:
    from docxtpl import DocxTemplate, RichText
except Exception as _e:  # noqa: BLE001
    DocxTemplate = None  # Defer import error until runtime for clear UX

# Local imports
import sys
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from scripts import notion_utils as nu


# ---------- Helpers: Property readers ----------

PATH_CACHE_FILE = project_root / "lha_paths.json"


def _get_rich_text(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""


def _get_title(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("title", [])
    return arr[0].get("plain_text", "") if arr else ""


def _get_number(props: Dict[str, Any], key: str) -> Optional[float]:
    v = props.get(key, {}).get("number")
    return float(v) if v is not None else None


def _get_url(props: Dict[str, Any], key: str) -> str:
    return props.get(key, {}).get("url") or ""


def _get_relation_ids(props: Dict[str, Any], key: str) -> List[str]:
    arr = props.get(key, {}).get("relation", [])
    return [x.get("id") for x in arr if isinstance(x, dict) and x.get("id")]


def _sanitize_filename(name: str) -> str:
    # Replace characters invalid on Windows/macOS/Linux filesystems
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    name = re.sub(r"_+", "_", name)
    name = name.strip("._")
    if len(name) > 80:
        name = name[:80].rstrip("._")
    return name or "Location"


def _normalize_address(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\r", "").replace("\n", ", ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",\s*", ", ", text)
    text = text.replace(", ,", ", ")
    return text.strip().strip(",")


def _compose_hours_str(props: Dict[str, Any]) -> str:
    # Facilities store weekday-specific hours as rich_text fields
    day_to_prop = {
        "Monday": "Monday Hours",
        "Tuesday": "Tuesday Hours",
        "Wednesday": "Wednesday Hours",
        "Thursday": "Thursday Hours",
        "Friday": "Friday Hours",
        "Saturday": "Saturday Hours",
        "Sunday": "Sunday Hours",
    }
    ordered_days = list(day_to_prop.keys())
    normalized: List[Tuple[str, str]] = []
    for day in ordered_days:
        value = _get_rich_text(props, day_to_prop[day]).strip()
        normalized.append((day, value or "Closed"))

    grouped: List[Dict[str, str]] = []  # each entry: {'start': day, 'end': day, 'hours': value}
    for day, hours in normalized:
        if not grouped or grouped[-1]["hours"] != hours:
            grouped.append({"start": day, "end": day, "hours": hours})
        else:
            grouped[-1]["end"] = day

    def label(start: str, end: str) -> str:
        return start if start == end else f"{start}-{end}"

    return "\n".join(
        f"{label(entry['start'], entry['end'])}: {entry['hours']}"
        for entry in grouped
    )


def _read_facility(page: dict) -> Dict[str, str]:
    props = page.get("properties", {})
    # Prefer explicit 'Location Name' if present, else try the title
    name = (
        _get_rich_text(props, "Name")
        or _get_rich_text(props, "Facility Name")
        or _get_rich_text(props, "Location Name")
        or _get_title(props, "Name")
        or _get_title(props, "Facility Name")
        or _get_title(props, "MedicalFacilityID")
        or _get_title(props, "Title")
    )

    address = (
        _get_rich_text(props, "Address")
        or _get_rich_text(props, "Full Address")
        or _get_rich_text(props, "Location Address")
    )
    address = _normalize_address(address)

    phone = (
        props.get("Phone", {}).get("phone_number")
        or props.get("International Phone", {}).get("phone_number")
        or _get_rich_text(props, "Phone")
        or _get_rich_text(props, "International Phone")
        or ""
    ).strip()

    website = (
        _get_url(props, "Website")
        or _get_url(props, "Site")
        or _get_url(props, "URL")
    )
    maps_url = _get_url(props, "Google Maps URL")

    hours = _compose_hours_str(props)
    return {
        "name": name or "",
        "address": address or "",
        "phone": phone or "",
        "hours": hours or "",
        "website": website or "",
        "maps_url": maps_url or "",
    }


# ---------- Core flow ----------

def _load_productions_map() -> Dict[str, str]:
    path = project_root / "notion_tables.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Invalid format in notion_tables.json")
            return data
    except Exception as e:  # noqa: BLE001
        print("\n[ERROR] Could not read notion_tables.json. Run sync first.")
        print(f"Reason: {e}")
        return {}


def _select_from_list(
    prompt: str,
    items: List[str],
    extra_options: Optional[Dict[str, str]] = None,
) -> Optional[object]:
    print(prompt)
    for i, label in enumerate(items, 1):
        print(f"[{i}] {label}")
    if extra_options:
        for key, description in extra_options.items():
            print(f"[{key.upper()}] {description}")

    while True:
        choice = input("> ").strip()
        if not choice:
            return None
        lowered = choice.lower()
        if extra_options and lowered in extra_options:
            return lowered
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(items):
                return idx - 1
        print("Invalid choice. Enter a number from the list or use one of the shortcuts shown.")


def _fetch_locations(db_id: str) -> List[dict]:
    print("\nFetching locations from Notion...")
    return nu.query_database(db_id)


def _display_locations(locations: List[dict]) -> List[str]:
    labels: List[str] = []
    for page in locations:
        props = page.get("properties", {})
        practical = _get_rich_text(props, "Practical Name")
        loc_name = _get_rich_text(props, "Location Name")
        address = _get_rich_text(props, "Full Address")
        label = practical or loc_name or address or page.get("id", "Unknown")
        # Include a small hint
        if practical and loc_name and practical != loc_name:
            label = f"{practical} (aka {loc_name})"
        labels.append(label)
    return labels


def _get_location_context(page: dict) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    props = page.get("properties", {})
    location_name = _get_rich_text(props, "Location Name")
    practical_name = _get_rich_text(props, "Practical Name")
    full_address = _get_rich_text(props, "Full Address")
    lat = _get_number(props, "Latitude")
    lng = _get_number(props, "Longitude")
    # Production abbreviation via rollup if present; otherwise fallback later
    production_abbrev = _get_rich_text(props, "Abbreviation")

    # Most of the URLs and facility links live on the Master page
    master_ids = _get_relation_ids(props, "LocationsMasterID")
    master_id = master_ids[0] if master_ids else None

    production_ids = _get_relation_ids(props, "ProductionID")
    production_page_id = production_ids[0] if production_ids else None

    base: Dict[str, Any] = {
        "location_name": location_name or "",
        "practical_name": practical_name or "",
        "full_address": _normalize_address(full_address),
        "latitude": lat if lat is not None else "",
        "longitude": lng if lng is not None else "",
        "production_abbrev": production_abbrev or "",
        # To be filled after master fetch
        "google_maps_url": "",
        "uc1_name": "", "uc1_address": "", "uc1_phone": "", "uc1_hours": "", "uc1_website": "", "uc1_maps_url": "",
        "uc2_name": "", "uc2_address": "", "uc2_phone": "", "uc2_hours": "", "uc2_website": "", "uc2_maps_url": "",
        "er_name": "",  "er_address": "",  "er_phone": "",  "er_hours": "",  "er_website": "", "er_maps_url": "",
    }
    return base, master_id, production_page_id


def _augment_with_master_data(
    ctx: Dict[str, Any],
    master_id: Optional[str],
    page_cache: Dict[str, Dict],
) -> None:
    if not master_id:
        print("[WARN] No LocationsMasterID relation found; skipping master lookups.")
        return
    try:
        if master_id in page_cache:
            master_page = page_cache[master_id]
        else:
            master_page = nu.get_page(master_id)
            page_cache[master_id] = master_page
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Could not fetch master page {master_id}: {e}")
        return

    mprops = master_page.get("properties", {})
    master_address = _get_rich_text(mprops, "Full Address") or _get_rich_text(mprops, "Address")
    if master_address and not ctx.get("full_address"):
        ctx["full_address"] = _normalize_address(master_address)

    ctx["google_maps_url"] = _get_url(mprops, "Google Maps URL") or ctx.get("google_maps_url", "")

    # Facilities relations on the master page
    def get_first(rel_name: str) -> Optional[str]:
        ids = _get_relation_ids(mprops, rel_name)
        return ids[0] if ids else None

    uc1_id = get_first("UC1")
    uc2_id = get_first("UC2")  # Optional
    er_id = get_first("ER")

    for key_prefix, page_id in (("uc1", uc1_id), ("uc2", uc2_id), ("er", er_id)):
        if not page_id:
            continue
        try:
            if page_id in page_cache:
                fac_page = page_cache[page_id]
            else:
                fac_page = nu.get_page(page_id)
                page_cache[page_id] = fac_page
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Could not fetch facility page {page_id}: {e}")
            continue
        details = _read_facility(fac_page)
        ctx[f"{key_prefix}_name"] = details.get("name", "")
        ctx[f"{key_prefix}_address"] = details.get("address", "")
        ctx[f"{key_prefix}_phone"] = details.get("phone", "")
        ctx[f"{key_prefix}_hours"] = details.get("hours", "")
        ctx[f"{key_prefix}_website"] = details.get("website", "")
        ctx[f"{key_prefix}_maps_url"] = details.get("maps_url", "")


def _augment_with_production_data(
    ctx: Dict[str, Any],
    production_id: Optional[str],
    page_cache: Dict[str, Dict],
) -> None:
    if not production_id:
        return
    try:
        if production_id in page_cache:
            prod_page = page_cache[production_id]
        else:
            prod_page = nu.get_page(production_id)
            page_cache[production_id] = prod_page
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Could not fetch production page {production_id}: {e}")
        return

    props = prod_page.get("properties", {})

    # Prefer an explicit Name property if present
    production_name = (
        _get_title(props, "Name")
        or _get_rich_text(props, "Name")
        or _get_title(props, Config.PROD_MASTER_TITLE_PROP or "ProductionID")
        or _get_rich_text(props, Config.PROD_MASTER_TITLE_PROP or "ProductionID")
    )
    if production_name:
        ctx["production_name"] = production_name

    abbr_prop = Config.PROD_MASTER_ABBR_PROP or "Abbreviation"
    production_abbrev = _get_rich_text(props, abbr_prop) or _get_title(props, abbr_prop)
    if production_abbrev:
        ctx["production_abbrev"] = production_abbrev


def _ensure_logs_file(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.is_file():
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "production_name",
                "production_abbrev",
                "location_page_id",
                "location_name",
                "practical_name",
                "output_file",
            ])


def _render_and_save(ctx: Dict[str, Any], output_folder: Path, production_name: str) -> Path:
    if DocxTemplate is None:
        raise RuntimeError("docxtpl is not installed. Please install 'docxtpl' and retry.")

    template_path = project_root / "templates" / "LHA_Template.docx"
    if not template_path.is_file():
        raise FileNotFoundError(f"Template not found: {template_path}")

    # Derive filename
    abbrev = ctx.get("production_abbrev") or production_name
    safe_loc_name = _sanitize_filename(ctx.get("practical_name") or ctx.get("location_name") or "Location")
    filename = f"{abbrev}_{safe_loc_name}_LHA.docx"

    print("\nRendering document...")
    doc = DocxTemplate(str(template_path))
    # Prepare rendering context with defaults and hyperlinks
    render_ctx: Dict[str, Any] = {}
    for key, value in ctx.items():
        if isinstance(value, (int, float)):
            render_ctx[key] = value
        else:
            render_ctx[key] = "" if value is None else str(value)

    if not render_ctx.get("production_name"):
        render_ctx["production_name"] = production_name
    if not render_ctx.get("production_abbrev"):
        render_ctx["production_abbrev"] = abbrev

    maps_url = ctx.get("google_maps_url")
    if maps_url:
        rt = RichText()
        rt.add("Open in Google Maps", url_id=doc.build_url_id(str(maps_url)))
        render_ctx["google_maps_link"] = rt
    else:
        render_ctx["google_maps_link"] = ""

    # Facility website hyperlinks if available
    for prefix in ("uc1", "uc2", "er"):
        website = ctx.get(f"{prefix}_website")
        if website:
            rt_link = RichText()
            rt_link.add("Website", url_id=doc.build_url_id(str(website)))
            render_ctx[f"{prefix}_website_link"] = rt_link
        else:
            render_ctx[f"{prefix}_website_link"] = ""

        maps_link = ctx.get(f"{prefix}_maps_url")
        if maps_link:
            rt_map = RichText()
            rt_map.add("Map", url_id=doc.build_url_id(str(maps_link)))
            render_ctx[f"{prefix}_maps_link"] = rt_map
        else:
            render_ctx[f"{prefix}_maps_link"] = ""

    doc.render(render_ctx)

    output_folder.mkdir(parents=True, exist_ok=True)
    output_path = output_folder / filename
    doc.save(str(output_path))
    return output_path


def _load_saved_paths() -> Dict[str, str]:
    if PATH_CACHE_FILE.is_file():
        try:
            data = PATH_CACHE_FILE.read_text(encoding="utf-8-sig")
            return json.loads(data)
        except json.JSONDecodeError:
            print("[WARN] Failed to parse lha_paths.json; starting fresh.")
        except OSError as exc:
            print(f"[WARN] Could not read lha_paths.json: {exc}")
    return {}


def _persist_saved_paths(data: Dict[str, str]) -> None:
    try:
        PATH_CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not persist lha_paths.json: {exc}")


def run() -> None:
    # 1) Setup config and basic checks
    Config.setup()
    if not Config.NOTION_TOKEN:
        print("[ERROR] NOTION_TOKEN is missing. Please configure your .env and retry.")
        return

    # 2) Load productions and prompt user
    table_map = _load_productions_map()
    if not table_map:
        return
    saved_paths = _load_saved_paths()
    page_cache: Dict[str, Dict] = {}

    prod_keys = list(table_map.keys())

    while True:
        print("\nSelect a production (Enter to cancel):")
        prod_choice = _select_from_list(
            "Available productions:",
            prod_keys,
            extra_options={
                "m": "Return to main menu",
            },
        )
        if prod_choice is None:
            print("Cancelled.")
            return
        if isinstance(prod_choice, str):
            if prod_choice == "m":
                print("Returning to main menu.")
                return
            continue

        selected_key = prod_keys[prod_choice]
        db_id = table_map[selected_key]

        # Derive production name/abbrev heuristically from key (e.g., 'AMCL_Locations')
        if selected_key.endswith("_Locations"):
            current_abbrev = selected_key.replace("_Locations", "")
            current_name = current_abbrev
        else:
            current_name = selected_key
            current_abbrev = selected_key

        print(f"\nFetching locations for production: {selected_key} ({db_id})")
        locations = _fetch_locations(db_id)
        if not locations:
            print("\nNo locations found for the selected production.")
            continue

        stored_path = saved_paths.get(current_abbrev)
        default_out = stored_path or str(project_root / "output")

        while True:
            labels = _display_locations(locations)
            print("\nSelect a location to generate an LHA:")
            loc_choice = _select_from_list(
                "Available locations:",
                labels,
                extra_options={
                    "s": "Switch production",
                    "m": "Return to main menu",
                },
            )
            if loc_choice is None:
                # Treat Enter as switch back to production menu
                break
            if isinstance(loc_choice, str):
                if loc_choice == "s":
                    break
                if loc_choice == "m":
                    print("Returning to main menu.")
                    return
                continue

            selected_page = locations[loc_choice]
            selected_label = labels[loc_choice]
            print(f"\nSelected: {selected_label}")

            # 3) Fetch detailed context
            print("Fetching location details...")
            ctx, master_id, production_page_id = _get_location_context(selected_page)
            # If production_abbrev was empty from rollup, fallback to derived
            if not ctx.get("production_abbrev"):
                ctx["production_abbrev"] = current_abbrev
            _augment_with_master_data(ctx, master_id, page_cache)
            _augment_with_production_data(ctx, production_page_id, page_cache)

            if ctx.get("production_name"):
                current_name = ctx["production_name"]
            if ctx.get("production_abbrev"):
                current_abbrev = ctx["production_abbrev"]

            if not ctx.get("latitude") or not ctx.get("longitude"):
                print("[WARN] Location is missing latitude/longitude; template may show blanks.")
            if not ctx.get("google_maps_url"):
                print("[WARN] No Google Maps URL found for this location.")

            # 4) Ask user for output folder
            stored_path = saved_paths.get(current_abbrev)
            if stored_path:
                print(f"\nExisting path for {current_abbrev}: {stored_path}")
                dest_str = input("Press Enter to reuse or type a new path: ").strip()
            else:
                print(f"\nEnter output folder for {current_abbrev} (default: {default_out}):")
                dest_str = input("> ").strip()

            output_folder = Path(dest_str) if dest_str else Path(default_out)
            resolved_folder = str(output_folder)
            if resolved_folder != saved_paths.get(current_abbrev):
                saved_paths[current_abbrev] = resolved_folder
                _persist_saved_paths(saved_paths)
                print(f"[INFO] Saved output path for {current_abbrev}: {resolved_folder}")
                default_out = resolved_folder
            else:
                print(f"[INFO] Using stored output path: {resolved_folder}")

            try:
                # 5) Render and save
                output_path = _render_and_save(ctx, output_folder, current_name)
                print(f"\n[OK] Document created: {output_path}")
            except Exception as e:  # noqa: BLE001
                print(f"\n[ERROR] Could not generate document: {e}")
                continue

            # 6) Append to log
            logs_dir = project_root / "logs"
            log_file = logs_dir / "lha_generation_log.csv"
            _ensure_logs_file(log_file)
            try:
                with open(log_file, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        datetime.utcnow().isoformat(timespec="seconds"),
                        current_name,
                        ctx.get("production_abbrev", ""),
                        selected_page.get("id", ""),
                        ctx.get("location_name", ""),
                        ctx.get("practical_name", ""),
                        str(output_path),
                    ])
                print(f"[LOG] Appended entry to {log_file}")
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Could not write log entry: {e}")

            # Refresh location list before next iteration in case Notion data changed
            locations = _fetch_locations(db_id)
            if not locations:
                print("\nNo locations available after refresh; returning to production selection.")
                break


if __name__ == "__main__":
    run()
