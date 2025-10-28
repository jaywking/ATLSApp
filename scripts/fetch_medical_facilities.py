# scripts/fetch_medical_facilities.py
"""
Fetch nearby medical facilities (Urgent Care and ER) for Locations Master entries.

What it does:
- Reads all Locations Master pages
- For each master page, fills all empty facility "slots" (UC1, UC2, UC3, ER) with the nearest
  Google Places result, avoiding duplicates across the facilities DB by Place_ID and within the same master row.
- Creates facility records in Medical Facilities DB (MF###), links them back to the master row,
  and writes the slot relation on the master page

Dependencies:
- scripts/google_utils.py (shared Google API helpers)
- scripts/notion_utils.py (Notion wrappers + format helpers)
- config.Config for DB IDs and status names

Adjust property names in the TUNABLES section if your schema differs.
"""

from __future__ import annotations
import csv
import logging
import sys
import time
import argparse
import threading
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from tqdm import tqdm

# Project imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from scripts import notion_utils as nu
from scripts.google_utils import nearby_places, place_details as google_place_details

# ─── TUNABLES ────────────────────────────────────────────────────────────────

# Locations Master DB property names
MASTER_LAT_PROP = "Latitude"
MASTER_LNG_PROP = "Longitude"
MASTER_TITLE_PROP = "LocationsMasterID"
MASTER_PLACEID_PROP = "Place_ID"

# Backlink relation on the Facilities DB pointing back to Master DB
FACILITIES_BACKLINK_PROP = "LocationsMasterID"  # default relation prop in Facilities DB back to master page

DAY_TO_PROP = {
    "Monday": "Monday Hours",
    "Tuesday": "Tuesday Hours",
    "Wednesday": "Wednesday Hours",
    "Thursday": "Thursday Hours",
    "Friday": "Friday Hours",
    "Saturday": "Saturday Hours",
    "Sunday": "Sunday Hours",
}

# How many candidates to scan per type; we only fill one per empty slot
NEARBY_SCAN_LIMIT = 8

# CSV log path
LOGS_DIR = project_root / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOGS_DIR / f"medical_facilities_log_{time.strftime('%Y-%m-%d_%H%M%S')}.csv"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_num(props: Dict[str, Any], key: str) -> Optional[float]:
    v = props.get(key, {}).get("number")
    return None if v is None else float(v)

def _get_title(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("title", [])
    return arr[0].get("plain_text", "") if arr else ""

def _get_relation_ids(props: Dict[str, Any], key: str) -> List[str]:
    arr = props.get(key, {}).get("relation", [])
    return [x.get("id") for x in arr if isinstance(x, dict) and "id" in x]

def _get_rich_text(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""

def _extract_phone(details: Dict[str, Any]) -> Optional[str]:
    phone = details.get("formatted_phone_number") or details.get("international_phone_number")
    if not isinstance(phone, str):
        return None
    phone = phone.strip()
    return phone or None

def _index_address_components(details: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for component in details.get("address_components", []) or []:
        for type_name in component.get("types", []):
            if type_name not in index:
                index[type_name] = component
    return index

def _component_value(component: Optional[Dict[str, Any]], *, prefer_short: bool = True) -> str:
    if not component:
        return ""
    if prefer_short and component.get("short_name"):
        return component["short_name"]
    return component.get("long_name") or component.get("short_name") or ""

def _component_tokens(component: Optional[Dict[str, Any]]) -> List[str]:
    tokens: List[str] = []
    if not component:
        return tokens
    short = component.get("short_name")
    long = component.get("long_name")
    if short:
        tokens.append(short)
    if long and long != short:
        tokens.append(long)
    if short and short.upper() == "US":
        tokens.extend(["USA", "United States"])
    return [t for t in tokens if t]

def _address_missing_required_tokens(address: str, components_index: Dict[str, Dict[str, Any]]) -> bool:
    if not address:
        return True
    normalized = address.lower()
    for key in ("administrative_area_level_1", "postal_code", "country"):
        component = components_index.get(key)
        if not component:
            continue
        if key == "country" and component.get("short_name", "").upper() == "US":
            continue
        variants = [token.lower() for token in _component_tokens(component)]
        if variants and not any(variant in normalized for variant in variants):
            return True
    return False

def _compose_complete_address(details: Dict[str, Any]) -> Optional[str]:
    components_index = _index_address_components(details)
    formatted = (details.get("formatted_address") or "").strip()
    formatted_tokens = [token.strip() for token in formatted.split(",") if token.strip()]

    street_number = _component_value(components_index.get("street_number"), prefer_short=True)
    route = _component_value(components_index.get("route"), prefer_short=True)
    street = " ".join(part for part in [street_number, route] if part).strip()

    subpremise = _component_value(components_index.get("subpremise"), prefer_short=False)
    premise = _component_value(components_index.get("premise"), prefer_short=False)
    if not subpremise and premise and premise.lower().startswith(("unit", "suite", "#")):
        subpremise = premise
    if subpremise:
        street = f"{street} {subpremise}".strip() if street else subpremise
    if not street and formatted_tokens:
        street = formatted_tokens[0]

    city = (
        _component_value(components_index.get("locality"), prefer_short=False)
        or _component_value(components_index.get("sublocality_level_1"), prefer_short=False)
        or _component_value(components_index.get("sublocality"), prefer_short=False)
        or _component_value(components_index.get("administrative_area_level_2"), prefer_short=False)
    )
    if not city and len(formatted_tokens) > 1:
        city = formatted_tokens[1]

    state = _component_value(components_index.get("administrative_area_level_1"), prefer_short=True)
    postal = _component_value(components_index.get("postal_code"), prefer_short=True)
    state_zip = " ".join(part for part in [state, postal] if part).strip()

    country_component = components_index.get("country")
    if country_component:
        if country_component.get("short_name", "").upper() == "US":
            country = ""
        else:
            country = _component_value(country_component, prefer_short=False)
    else:
        country = formatted_tokens[-1] if len(formatted_tokens) >= 3 else ""

    parts = [part for part in [street, city, state_zip, country] if part]
    if parts:
        return ", ".join(parts)
    return formatted or None

def _build_full_address(details: Dict[str, Any]) -> Optional[str]:
    components_index = _index_address_components(details)
    formatted = (details.get("formatted_address") or "").strip()
    composed = _compose_complete_address(details)
    candidates = [candidate for candidate in [composed, formatted] if candidate]

    if components_index:
        for candidate in candidates:
            if candidate and not _address_missing_required_tokens(candidate, components_index):
                return candidate
    return candidates[0] if candidates else None

def _address_seems_incomplete(address: str) -> bool:
    if not address:
        return True
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 3:
        return True
    # Expect U.S. addresses to end with "... , STATE ZIP, COUNTRY"
    # If a country component exists, the state/zip portion is the second-to-last part.
    state_zip_candidate = parts[-1]
    if len(parts) >= 4:
        state_zip_candidate = parts[-2]
    state_match = re.search(r"\b[A-Z]{2}\b", state_zip_candidate)
    zip_match = re.search(r"\b\d{5}(?:-\d{4})?\b", state_zip_candidate)
    if not state_match or not zip_match:
        return True
    return False

def _maybe_get_better_address(existing_value: str, details: Dict[str, Any]) -> Optional[str]:
    full_address = _build_full_address(details)
    if not full_address:
        return None
    current = (existing_value or "").strip()
    if not current:
        return full_address
    if current.lower() == full_address.lower():
        return None
    components_index = _index_address_components(details)
    if components_index and _address_missing_required_tokens(current, components_index):
        return full_address
    if len(full_address) > len(current):
        return full_address
    return None

def _facility_place_ids_in_db(facilities_db: str) -> Dict[str, str]:
    """
    Return dict of {place_id: facility_page_id} for all existing facility pages.
    Used to avoid creating duplicates across the Facilities DB.
    """
    out: Dict[str, str] = {}
    pages = nu.query_database(facilities_db)
    for p in pages:
        props = p.get("properties", {})
        place_id_arr = props.get(MASTER_PLACEID_PROP, {}).get("rich_text", [])
        if place_id_arr:
            pid = place_id_arr[0].get("plain_text", "")
            if pid:
                out[pid] = p["id"]
    return out

def _slot_search_params(slot_name: str) -> Tuple[str, Optional[str]]:
    """
    Return (keyword, type_hint) for the given slot.
    You can tailor this map if your Google search strategy changes.
    """
    slot_upper = slot_name.upper()
    if slot_upper.startswith("UC"):
        return ("urgent care", None)           # 'urgent care' nearby
    if slot_upper == "ER":
        return ("emergency room", "hospital")  # ERs are generally hospitals
    # Fallback generic
    return ("urgent care", None)

def _append_log_row(row: Dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        exists = LOG_PATH.exists()
        with open(LOG_PATH, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "timestamp", "master_id", "master_page_id", "slot",
                "facility_page_id", "facility_title", "place_id",
                "name", "phone", "website", "maps_url", "lat", "lng", "note"
            ])
            if not exists:
                w.writeheader()
            w.writerow(row)

def _discover_backlink_prop(facilities_db_id: str, master_db_id: str) -> Optional[str]:
    """
    Try to discover the Facilities→Master backlink relation property by inspecting the
    Facilities DB schema and finding a relation that targets the Master DB.
    Falls back to FACILITIES_BACKLINK_PROP if none found.
    """
    try:
        db = nu.get_database(facilities_db_id)
        for name, meta in (db.get("properties") or {}).items():
            if meta.get("type") == "relation":
                rel = meta.get("relation") or {}
                if rel.get("database_id") == master_db_id:
                    return name
    except Exception:
        pass
    return None

def _enrich_facility_from_google_if_needed(
    facility_page_id: str,
    facility_props: Dict[str, Any],
    dry_run: bool = False
) -> bool:
    """
    If Address / Website / Maps URL / Phone / Type / Hours are missing on a facility page,
    and a Place_ID exists, fetch Google Place Details and update the missing fields.
    Returns True if an update was applied.
    """
    place_id = _get_rich_text(facility_props, "Place_ID")
    if not place_id:
        return False

    # Decide if we need details at all
    needs_any = False
    name_existing = _get_rich_text(facility_props, "Name")
    address_existing = _get_rich_text(facility_props, "Address")
    phone_existing = _get_rich_text(facility_props, "Phone")

    if not name_existing or not address_existing or not phone_existing:
        needs_any = True
    elif _address_seems_incomplete(address_existing):
        needs_any = True

    if not needs_any:
        # Check URL fields and hours
        if not (facility_props.get("Website", {}).get("url") and facility_props.get("Google Maps URL", {}).get("url")):
            needs_any = True
        else:
            for prop in DAY_TO_PROP.values():
                if not _get_rich_text(facility_props, prop):
                    needs_any = True
                    break

    if not needs_any:
        return False

    details = google_place_details(place_id, [
        "name","types","url","international_phone_number","formatted_phone_number","website","formatted_address","address_components","opening_hours"
    ])

    update_props: Dict[str, Any] = {}
    def ensure_rich(prop_name: str, value: Optional[str]) -> None:
        if value and not _get_rich_text(facility_props, prop_name):
            update_props[prop_name] = nu.format_rich_text(value)

    ensure_rich("Name", details.get("name"))

    phone_value = _extract_phone(details)
    if phone_value and not _get_rich_text(facility_props, "Phone"):
        update_props["Phone"] = nu.format_rich_text(phone_value)

    new_address = _maybe_get_better_address(_get_rich_text(facility_props, "Address"), details)
    if new_address:
        update_props["Address"] = nu.format_rich_text(new_address)

    if details.get("website") and not facility_props.get("Website", {}).get("url"):
        update_props["Website"] = nu.format_url(details["website"])
    if details.get("url") and not facility_props.get("Google Maps URL", {}).get("url"):
        update_props["Google Maps URL"] = nu.format_url(details["url"])
    if details.get("types") and not facility_props.get("Type", {}).get("select"):
        first_type = next((t for t in details["types"] if t not in ["point_of_interest", "establishment"]), None)
        if first_type:
            update_props["Type"] = nu.format_select(first_type.replace("_", " ").title()[:100])

    weekday_text = (details.get("opening_hours") or {}).get("weekday_text") or []
    for entry in weekday_text:
        if ":" not in entry:
            continue
        day, hours = entry.split(":", 1)
        day = day.strip()
        hours = hours.strip()
        prop_name = DAY_TO_PROP.get(day)
        if prop_name and not _get_rich_text(facility_props, prop_name):
            update_props[prop_name] = nu.format_rich_text(hours)

    if update_props:
        if dry_run:
            print(f"  [DRY-RUN] Would enrich facility '{facility_page_id}' with {list(update_props.keys())}.")
        else:
            nu.update_page(facility_page_id, update_props)
        return True
    return False

def _backfill_existing_links_and_details(
    master_db_id: str,
    facilities_db_id: str,
    facility_slots: List[str],
    dry_run: bool = False
) -> Tuple[int, int]:
    """
    Optimized backfill:
    - Deduplicate facilities by scanning master pages once and grouping by facility page ID.
    - Bulk-load facility properties from Facilities DB to avoid per-page GETs.
    - Fetch Google Place Details in parallel only for facilities missing details and with Place_ID.
    - Apply Notion updates with gentle concurrency to respect rate limits.
    Returns (links_fixed, facilities_enriched).
    """
    print("\nStarting backfill: repairing backlink relations and enriching details (optimized)...")
    backlink_prop = _discover_backlink_prop(facilities_db_id, master_db_id) or FACILITIES_BACKLINK_PROP
    if backlink_prop != FACILITIES_BACKLINK_PROP:
        print(f"  - Using discovered backlink property: '{backlink_prop}'")
    else:
        print(f"  - Using default backlink property: '{FACILITIES_BACKLINK_PROP}'")

    # 1) Build facility -> set(master_page_ids) map by scanning master pages (dedup)
    facility_to_masters: Dict[str, set] = {}
    master_pages = nu.query_database(master_db_id)
    for master_page in tqdm(master_pages, desc="Scanning master pages"):
        mprops = master_page.get("properties", {})
        mid = master_page.get("id")
        # Collect all linked facility IDs across slots
        for slot in facility_slots:
            for fac_id in _get_relation_ids(mprops, slot):
                facility_to_masters.setdefault(fac_id, set()).add(mid)

    if not facility_to_masters:
        print("Nothing to backfill: no linked facilities found.")
        return 0, 0

    # 2) Bulk-load Facilities DB and index by page ID
    facilities_pages = nu.query_database(facilities_db_id)
    fac_props_by_id: Dict[str, Dict[str, Any]] = {p.get("id"): p.get("properties", {}) for p in facilities_pages}

    # Ensure we have props for every linked facility; fallback to get_page for any missing
    missing_fac_ids = [fid for fid in facility_to_masters.keys() if fid not in fac_props_by_id]
    for fid in missing_fac_ids:
        try:
            fac_page = nu.get_page(fid)
            fac_props_by_id[fid] = fac_page.get("properties", {})
        except Exception as e:
            print(f"  - Unable to fetch facility {fid}: {e}. Skipping.")

    # Helpers to detect missing details and build updates from details
    def _facility_missing_any(props: Dict[str, Any]) -> bool:
        name_val = _get_rich_text(props, "Name")
        address_val = _get_rich_text(props, "Address")
        phone_val = _get_rich_text(props, "Phone")
        if not name_val or not address_val or not phone_val:
            return True
        if _address_seems_incomplete(address_val):
            return True
        if not (props.get("Website", {}).get("url") and props.get("Google Maps URL", {}).get("url")):
            return True
        if not props.get("Type", {}).get("select"):
            return True
        for prop in DAY_TO_PROP.values():
            if not _get_rich_text(props, prop):
                return True
        return False

    def _build_updates_from_details(props: Dict[str, Any], details: Dict[str, Any]) -> Dict[str, Any]:
        updates: Dict[str, Any] = {}
        def ensure_rich(prop_name: str, value: Optional[str]) -> None:
            if value and not _get_rich_text(props, prop_name):
                updates[prop_name] = nu.format_rich_text(value)
        ensure_rich("Name", details.get("name"))
        phone_val = _extract_phone(details)
        if phone_val and not _get_rich_text(props, "Phone"):
            updates["Phone"] = nu.format_rich_text(phone_val)
        new_address = _maybe_get_better_address(_get_rich_text(props, "Address"), details)
        if new_address:
            updates["Address"] = nu.format_rich_text(new_address)
        if details.get("website") and not props.get("Website", {}).get("url"):
            updates["Website"] = nu.format_url(details["website"])
        if details.get("url") and not props.get("Google Maps URL", {}).get("url"):
            updates["Google Maps URL"] = nu.format_url(details["url"])
        if details.get("types") and not props.get("Type", {}).get("select"):
            first_type = next((t for t in details["types"] if t not in ["point_of_interest", "establishment"]), None)
            if first_type:
                updates["Type"] = nu.format_select(first_type.replace("_", " ").title()[:100])
        weekday_text = (details.get("opening_hours") or {}).get("weekday_text") or []
        for entry in weekday_text:
            if ":" not in entry:
                continue
            day, hours = entry.split(":", 1)
            day = day.strip(); hours = hours.strip()
            prop_name = DAY_TO_PROP.get(day)
            if prop_name and not _get_rich_text(props, prop_name):
                updates[prop_name] = nu.format_rich_text(hours)
        return updates

    # 3) Prepare backlink and details worklists
    links_fixed = 0
    facilities_needing_details: List[str] = []
    backlink_updates: Dict[str, Dict[str, Any]] = {}

    for fid, masters in facility_to_masters.items():
        props = fac_props_by_id.get(fid) or {}

        # Backlink update calculation
        if backlink_prop in props:
            existing_rel = set(_get_relation_ids(props, backlink_prop))
            missing = masters - existing_rel
            if missing:
                backlink_updates[fid] = {backlink_prop: nu.format_relation(list(existing_rel | masters))}
        else:
            # Property missing on this page schema; skip quietly
            pass

        # Decide if details enrichment is needed
        place_id = _get_rich_text(props, "Place_ID")
        if place_id and _facility_missing_any(props):
            facilities_needing_details.append(fid)

    # 4) Fetch Google details in parallel for those that need it
    details_by_fid: Dict[str, Dict[str, Any]] = {}
    if facilities_needing_details:
        print(f"  - Fetching Google details for {len(facilities_needing_details)} facility(ies)...")
        with ThreadPoolExecutor(max_workers=10) as exec_details:
            future_map = {}
            for fid in facilities_needing_details:
                props = fac_props_by_id.get(fid) or {}
                pid = _get_rich_text(props, "Place_ID")
                if not pid:
                    continue
                future = exec_details.submit(google_place_details, pid, [
                    "name","types","url","international_phone_number","formatted_phone_number","website","formatted_address","address_components","opening_hours"
                ])
                future_map[future] = fid
            for fut in as_completed(future_map):
                fid = future_map[fut]
                try:
                    details_by_fid[fid] = fut.result()
                except Exception as e:
                    print(f"  - Details fetch failed for {fid}: {e}")

    # 5) Apply updates (backlinks + details) with gentle concurrency
    enriched = 0
    def _build_update_payload(fid: str) -> Optional[Dict[str, Any]]:
        props = fac_props_by_id.get(fid) or {}
        payload: Dict[str, Any] = {}
        # Backlink merge
        if fid in backlink_updates:
            payload.update(backlink_updates[fid])
        # Details-based enrichment
        if fid in details_by_fid:
            updates = _build_updates_from_details(props, details_by_fid[fid])
            if updates:
                payload.update(updates)
        return payload or None

    updates_to_apply: Dict[str, Dict[str, Any]] = {}
    for fid in set(list(backlink_updates.keys()) + list(details_by_fid.keys())):
        payload = _build_update_payload(fid)
        if payload:
            updates_to_apply[fid] = payload

    print(f"  - Prepared {len(updates_to_apply)} Notion page update(s). Applying...")
    if dry_run:
        for fid, payload in updates_to_apply.items():
            keys = ", ".join(payload.keys())
            print(f"  [DRY-RUN] Would update facility {fid} with: {keys}")
        # Count metrics
        links_fixed = sum(1 for fid in updates_to_apply if fid in backlink_updates)
        enriched = sum(1 for fid in updates_to_apply if fid in details_by_fid)
        print(f"Backfill complete. Links fixed: {links_fixed}, facilities enriched: {enriched}")
        return links_fixed, enriched

    # Real updates with small concurrency to respect rate limits
    def _apply(fid: str, payload: Dict[str, Any]) -> Tuple[str, bool, bool]:
        nu.update_page(fid, payload)
        return fid, (fid in backlink_updates), (fid in details_by_fid)

    with ThreadPoolExecutor(max_workers=3) as exec_update:
        futures = {exec_update.submit(_apply, fid, payload): fid for fid, payload in updates_to_apply.items()}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Applying updates"):
            try:
                fid, link_changed, detail_changed = fut.result()
                if link_changed:
                    links_fixed += 1
                if detail_changed:
                    enriched += 1
            except Exception as e:
                fid = futures[fut]
                print(f"  - Update failed for {fid}: {e}")

    print(f"Backfill complete. Links fixed: {links_fixed}, facilities enriched: {enriched}")
    return links_fixed, enriched

def _get_all_free_slots(props: Dict[str, Any], facility_slots: List[str]) -> List[str]:
    """Return all slots from FACILITY_SLOTS that are empty on the master row."""
    free_slots = []
    for slot in facility_slots:
        current = _get_relation_ids(props, slot)
        if not current:
            free_slots.append(slot)
    return free_slots

# ─── Core logic ──────────────────────────────────────────────────────────────

def _create_or_get_facility_page(
    facilities_db: str,
    existing_by_place: Dict[str, str],
    place_id: str,
    lat: float,
    lng: float,
    details: Dict[str, Any],
    next_mfid_counter: Dict[str, int],
    master_page_id: str,
    dry_run: bool = False
) -> Tuple[str, bool]:
    """
    Ensure there is a facility page for place_id, returning its page_id.
    Will create new page if it doesn't exist, linking it back to the master page.
    """
    # If a Facility record already exists for this Place_ID, refresh missing fields/relations
    if place_id and place_id in existing_by_place:
        page_id = existing_by_place[place_id]
        update_props: Dict[str, Any] = {}
        facility_props: Dict[str, Any] = {}
        try:
            facility_props = nu.get_page(page_id).get("properties", {})
        except Exception:
            facility_props = {}

        existing_rel_ids = _get_relation_ids(facility_props, FACILITIES_BACKLINK_PROP)
        if master_page_id and master_page_id not in existing_rel_ids:
            update_props[FACILITIES_BACKLINK_PROP] = nu.format_relation(existing_rel_ids + [master_page_id])

        def ensure_rich(prop_name: str, value: Optional[str]) -> None:
            if value and not _get_rich_text(facility_props, prop_name):
                update_props[prop_name] = nu.format_rich_text(value)

        ensure_rich("Name", details.get("name"))

        phone_value = _extract_phone(details)
        if phone_value and not _get_rich_text(facility_props, "Phone"):
            update_props["Phone"] = nu.format_rich_text(phone_value)

        updated_address = _maybe_get_better_address(_get_rich_text(facility_props, "Address"), details)
        if updated_address:
            update_props["Address"] = nu.format_rich_text(updated_address)

        if details.get("website") and not facility_props.get("Website", {}).get("url"):
            update_props["Website"] = nu.format_url(details["website"])
        if details.get("url") and not facility_props.get("Google Maps URL", {}).get("url"):
            update_props["Google Maps URL"] = nu.format_url(details["url"])
        if details.get("types") and not facility_props.get("Type", {}).get("select"):
            first_type = next((t for t in details["types"] if t not in ["point_of_interest", "establishment"]), None)
            if first_type:
                update_props["Type"] = nu.format_select(first_type.replace("_", " ").title()[:100])

        weekday_text = (details.get("opening_hours") or {}).get("weekday_text") or []
        for entry in weekday_text:
            if ":" not in entry:
                continue
            day, hours = entry.split(":", 1)
            day = day.strip()
            hours = hours.strip()
            prop_name = DAY_TO_PROP.get(day)
            if prop_name and not _get_rich_text(facility_props, prop_name):
                update_props[prop_name] = nu.format_rich_text(hours)

        if update_props:
            if dry_run:
                print(f"  [DRY-RUN] Would update facility '{page_id}' with {list(update_props.keys())}.")
            else:
                nu.update_page(page_id, update_props)
        return page_id, False

    # Create a new facility page
    # Atomically get the next available Medical Facility ID from the shared counter
    next_id_num = next_mfid_counter["value"]
    mfid = f"MF{str(next_id_num).zfill(3)}"
    next_mfid_counter["value"] += 1

    props = {
        "MedicalFacilityID": nu.format_title(mfid),
        "Place_ID": nu.format_rich_text(place_id or ""),
        "Latitude": nu.format_number(lat),
        "Longitude": nu.format_number(lng),
        FACILITIES_BACKLINK_PROP: nu.format_relation(master_page_id),
    }
    # Enrich with details if present
    if details.get("name"):
        props["Name"] = nu.format_rich_text(details["name"])
    full_address = _build_full_address(details)
    if full_address:
        props["Address"] = nu.format_rich_text(full_address)
    phone_value = _extract_phone(details)
    if phone_value:
        props["Phone"] = nu.format_rich_text(phone_value)
    if details.get("website"):
        props["Website"] = nu.format_url(details["website"])
    if details.get("url"):
        props["Google Maps URL"] = nu.format_url(details["url"])
    if details.get("types"):
        first_type = next((t for t in details["types"] if t not in ["point_of_interest", "establishment"]), None)
        if first_type:
            props["Type"] = nu.format_select(first_type.replace("_", " ").title()[:100])
    weekday_text = (details.get("opening_hours") or {}).get("weekday_text") or []
    for entry in weekday_text:
        if ":" not in entry:
            continue
        day, hours = entry.split(":", 1)
        day = day.strip()
        hours = hours.strip()
        prop_name = DAY_TO_PROP.get(day)
        if prop_name:
            props[prop_name] = nu.format_rich_text(hours)

    if dry_run:
        facility_name = details.get("name", "Unknown Name")
        print(f"  [DRY-RUN] Would create facility '{mfid}' for '{facility_name}' ({place_id})")
        new_id = f"DRY_RUN_PAGE_{mfid}"
    else:
        new_page = nu.create_page(facilities_db, props)
        new_id = new_page.get("id")

    if place_id:
        existing_by_place[place_id] = new_id  # update the in-memory cache
    return new_id, True # Return new page_id, did create

def _fetch_details_for_candidates(
    candidates: Dict[str, Dict[str, Any]],
    locks: Dict[str, threading.Lock]
) -> Dict[str, Dict[str, Any]]:
    """Fetches Google Place Details for a list of unique place_ids in parallel."""
    enriched_details = {}
    place_ids_to_fetch = list(candidates.keys())

    with ThreadPoolExecutor(max_workers=10) as detail_executor:
        future_to_pid = {
            detail_executor.submit(
                google_place_details,
                pid,
                ["name","types","url","international_phone_number","formatted_phone_number","website","formatted_address","address_components","opening_hours"]
            ): pid for pid in place_ids_to_fetch
        }
        for future in as_completed(future_to_pid):
            pid = future_to_pid[future]
            enriched_details[pid] = future.result()
    return enriched_details

def _fill_slots_for_master(
    master_db_id: str,
    facilities_db: str,
    master_page: Dict[str, Any],
    existing_by_place: Dict[str, str],
    locks: Dict[str, threading.Lock],
    next_mfid_counter: Dict[str, int],
    facility_slots: List[str],
    dry_run: bool = False
) -> int:
    """
    Try to fill ALL empty facility slots for the given master page.
    Returns the number of slots that were filled.
    """
    props = master_page["properties"]
    master_page_id = master_page["id"]
    master_id = _get_title(props, MASTER_TITLE_PROP) or master_page_id

    free_slots = _get_all_free_slots(props, facility_slots)
    if not free_slots:
        with locks["print"]:
            print(f"\n--- Processing master location: {master_id} ---")
            print("  - Skipping, all facility slots are already filled.")
        return 0

    lat = _get_num(props, MASTER_LAT_PROP)
    lng = _get_num(props, MASTER_LNG_PROP)
    if lat is None or lng is None:
        with locks["print"]:
            print(f"\n--- Processing master location: {master_id} ---")
            print("  - Skipping, missing coordinates on master record.")
        _append_log_row({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "master_id": master_id,
            "master_page_id": master_page_id, "slot": ", ".join(free_slots),
            "note": "Missing coordinates on master", "lat": "", "lng": "",
            "facility_page_id": "", "facility_title": "", "place_id": "", "name": "",
            "phone": "", "website": "", "maps_url": "",
        }, locks["csv"])
        return 0

    with locks["print"]:
        print(f"\n--- Processing master location: {master_id} ---")
        print(f"  - Found {len(free_slots)} empty slot(s): {', '.join(free_slots)}")

    filled_count = 0
    
    # Build a reverse map to find place_id from facility page_id
    page_id_to_place_id = {v: k for k, v in existing_by_place.items()}
    
    # Collect place_ids of facilities already linked to this master location
    used_place_ids = {
        page_id_to_place_id.get(rel_id)
        for slot in facility_slots
        for rel_id in _get_relation_ids(props, slot)
        if rel_id in page_id_to_place_id
    }

    # --- Phase 1: Find the best candidate for each slot without fetching details yet ---
    slot_candidates: Dict[str, Dict[str, Any]] = {}

    for slot in free_slots:
        keyword, type_hint = _slot_search_params(slot)
        try:
            # We only scan the first few results, so there's no need to fetch all 3 pages (60 results).
            # Limiting to one page (max 20 results) is much more efficient.
            results = nearby_places(
                lat, lng, keyword=keyword, place_type=type_hint,
                rankby="distance", max_pages=1
            )
            with locks["print"]:
                print(f"  - For slot '{slot}', found {len(results)} nearby candidates.")
        except Exception as e:
            _append_log_row({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "master_id": master_id,
                "master_page_id": master_page_id, "slot": slot, "note": f"Nearby search error: {e}",
                "lat": lat, "lng": lng, "facility_page_id": "", "facility_title": "", "place_id": "",
                "name": "", "phone": "", "website": "", "maps_url": "",
            }, locks["csv"])
            continue # Try next slot

        if not results:
            _append_log_row({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "master_id": master_id,
                "master_page_id": master_page_id, "slot": slot, "note": "No nearby results",
                "lat": lat, "lng": lng, "facility_page_id": "", "facility_title": "", "place_id": "",
                "name": "", "phone": "", "website": "", "maps_url": "",
            }, locks["csv"])
            continue

        chosen = None
        for candidate_result in results[:NEARBY_SCAN_LIMIT]:
            pid = candidate_result.get("place_id")
            if pid and pid not in used_place_ids:
                chosen = candidate_result
                break
        
        if not chosen:
            _append_log_row({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "master_id": master_id,
                "master_page_id": master_page_id, "slot": slot, "note": "No acceptable candidate among results",
                "lat": lat, "lng": lng, "facility_page_id": "", "facility_title": "", "place_id": "",
                "name": "", "phone": "", "website": "", "maps_url": "",
            }, locks["csv"])
        else:
            # Store the candidate and mark its place_id as used for the next slots in this run
            slot_candidates[slot] = chosen
            used_place_ids.add(chosen.get("place_id"))

    if not slot_candidates:
        return 0 # No new candidates found for any slot

    # --- Phase 2: Fetch details for all unique candidates in parallel ---
    unique_candidates_by_pid = {c.get("place_id"): c for c in slot_candidates.values() if c.get("place_id")}
    enriched_details = _fetch_details_for_candidates(unique_candidates_by_pid, locks)

    # --- Phase 3: Create/update Notion pages with the now-enriched details ---
    for slot, candidate in slot_candidates.items():
        pid = candidate.get("place_id")
        if not pid: continue

        details = enriched_details.get(pid, {})

        # Lock this section to prevent race conditions when creating/caching facility pages
        with locks["facility_cache"]:
            facility_page_id, created = _create_or_get_facility_page(
                facilities_db=facilities_db, existing_by_place=existing_by_place,
                place_id=pid, lat=candidate.get("geometry", {}).get("location", {}).get("lat", lat),
                lng=candidate.get("geometry", {}).get("location", {}).get("lng", lng),
                details=details, master_page_id=master_page_id, dry_run=dry_run,
                next_mfid_counter=next_mfid_counter,
            )

        if facility_page_id:
            if not dry_run:
                nu.update_page(master_page_id, {slot: nu.format_relation(facility_page_id)})
                with locks["print"]:
                    print(f"  -> Linked {slot} to facility {facility_page_id}")
            else:
                with locks["print"]:
                    print(f"  [DRY-RUN] Would update relation {slot} for location {master_page_id} → facility {facility_page_id}")

            filled_count += 1
            _append_log_row({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "master_id": master_id,
                "master_page_id": master_page_id, "slot": slot, "facility_page_id": facility_page_id,
                "facility_title": "", "place_id": pid, "name": details.get("name", ""),
                "phone": details.get("international_phone_number", ""), "website": details.get("website", ""),
                "maps_url": details.get("url", ""), "lat": lat, "lng": lng,
                "note": "linked (dry-run)" if dry_run else "linked",
            }, locks["csv"])

    return filled_count

def _discover_facility_slots(db_id: str) -> List[str]:
    """
    Inspects the database schema to dynamically find facility slot properties.
    This avoids hardcoding property names and makes the script more robust.
    """
    print("Discovering facility slots from database schema...")
    db_info = nu.get_database(db_id)
    props = db_info.get("properties", {})
    discovered_slots = []
    for name, meta in props.items():
        if meta.get("type") == "relation" and (name.upper().startswith("UC") or name.upper() == "ER"):
            discovered_slots.append(name)
    
    print(f"  - Found slots: {', '.join(discovered_slots) if discovered_slots else 'None'}")
    return discovered_slots

# ─── CLI runner ──────────────────────────────────────────────────────────────

def run_facility_refresh(
    master_ids: Optional[set[str]] = None,
    *,
    dry_run: bool = False,
    backfill_existing: bool = False,
) -> None:
    if dry_run:
        print("\n*** DRY RUN MODE ENABLED: No changes will be made to Notion. ***\n")

    if not Config.NOTION_TOKEN:
        print("? NOTION_TOKEN missing in .env. Aborting.")
        return
    if not Config.LOCATIONS_MASTER_DB or not Config.MEDICAL_FACILITIES_DB:
        print("? Missing LOCATIONS_MASTER_DB or MEDICAL_FACILITIES_DB in .env. Aborting.")
        return

    facility_slots = _discover_facility_slots(Config.LOCATIONS_MASTER_DB)
    if not facility_slots:
        print("? No facility slot properties (e.g., 'UC1', 'ER') found in the Locations Master database.")
        return

    if backfill_existing:
        _backfill_existing_links_and_details(
            master_db_id=Config.LOCATIONS_MASTER_DB,
            facilities_db_id=Config.MEDICAL_FACILITIES_DB,
            facility_slots=facility_slots,
            dry_run=dry_run,
        )
        if master_ids is None:
            return

    print("Building cache of existing facilities...")
    all_facility_pages = nu.query_database(Config.MEDICAL_FACILITIES_DB)

    existing_by_place: Dict[str, str] = {}
    max_mf_num = 0
    for p in all_facility_pages:
        props = p.get("properties", {})
        place_id_arr = props.get(MASTER_PLACEID_PROP, {}).get("rich_text", [])
        if place_id_arr:
            pid = place_id_arr[0].get("plain_text", "")
            if pid:
                existing_by_place[pid] = p["id"]

        title = _get_title(props, "MedicalFacilityID")
        if title.startswith("MF") and title[2:].isdigit():
            max_mf_num = max(max_mf_num, int(title[2:]))

    master_pages = nu.query_database(Config.LOCATIONS_MASTER_DB)
    if not master_pages:
        print("No master locations found.")
        return
    if master_ids:
        ids = set(master_ids)
        master_pages = [page for page in master_pages if page.get("id") in ids]
        if not master_pages:
            print("No matching master locations found for the requested IDs.")
            return

    next_mfid_counter = {"value": max_mf_num + 1}
    locks = {"print": threading.Lock(), "facility_cache": threading.Lock(), "csv": threading.Lock()}

    filled_slots_counter = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                _fill_slots_for_master,
                Config.LOCATIONS_MASTER_DB,
                Config.MEDICAL_FACILITIES_DB,
                page,
                existing_by_place,
                locks,
                next_mfid_counter,
                facility_slots,
                dry_run
            ): page for page in master_pages
        }

        for future in tqdm(as_completed(futures), total=len(master_pages), desc="Processing Master Locations"):
            try:
                filled_count = future.result()
                if filled_count > 0:
                    filled_slots_counter += filled_count
            except Exception as e:
                page = futures[future]
                tqdm.write(f"? An unexpected error occurred for page {page.get('id')}: {e}")
                props = page.get("properties", {})
                master_id = _get_title(props, MASTER_TITLE_PROP) or page.get("id", "")
                _append_log_row({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "master_id": master_id,
                    "master_page_id": page.get("id", ""),
                    "note": f"ERROR: {e}",
                    "slot": "", "facility_page_id": "", "facility_title": "", "place_id": "",
                    "name": "", "phone": "", "website": "", "maps_url": "", "lat": "", "lng": "",
                }, locks["csv"])

    print(f"\n[OK] Done. Filled {filled_slots_counter} slot(s). Log: {LOG_PATH}")



def main():
    logging.basicConfig(level=logging.INFO)
    load_dotenv(dotenv_path=project_root / '.env')

    parser = argparse.ArgumentParser(description="Fetch and link nearby medical facilities for master locations.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any changes to Notion."
    )
    parser.add_argument(
        "--backfill-existing",
        action="store_true",
        help="Backfill existing facility pages: repair backlink relation and enrich missing Address/Hours."
    )
    args = parser.parse_args()

    Config.setup()
    run_facility_refresh(dry_run=args.dry_run, backfill_existing=args.backfill_existing)


def run_backfill(dry_run: bool = False) -> None:
    """
    Programmatic entrypoint to run the backfill maintenance:
    - discovers facility slots on the master DB
    - repairs missing facility→master backlinks
    - enriches missing Address/Hours/URLs/Phone/Type from Google, when Place_ID exists

    Assumes Config.setup() has already been called by the launcher (e.g., run.py).
    """
    if not Config.NOTION_TOKEN:
        print("? NOTION_TOKEN missing in .env. Aborting.")
        return
    if not Config.LOCATIONS_MASTER_DB or not Config.MEDICAL_FACILITIES_DB:
        print("? Missing LOCATIONS_MASTER_DB or MEDICAL_FACILITIES_DB in .env. Aborting.")
        return

    slots = _discover_facility_slots(Config.LOCATIONS_MASTER_DB)
    if not slots:
        print("? No facility slot properties (e.g., 'UC1', 'ER') found in the Locations Master database.")
        return

    _backfill_existing_links_and_details(
        master_db_id=Config.LOCATIONS_MASTER_DB,
        facilities_db_id=Config.MEDICAL_FACILITIES_DB,
        facility_slots=slots,
        dry_run=dry_run,
    )
