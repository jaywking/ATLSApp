# scripts/match_location_master.py
"""
Match geocoded production rows to the Locations Master database.

Flow per row:
- Preconditions (default): Status == "Geocoded" (or your ready status) AND LocationsMasterID is empty
- If Place_ID exists -> link to existing master with same Place_ID
- Else try proximity match within ~10 m
- If still no match -> create a new master LOC### at that lat/lng (optionally enriched by Google Place Details if Place_ID is present)
- Update the production row with relation + keep status consistent

Features:
- --db-name: select a specific production DB (name from notion_tables.json)
- --all: process all rows that have no LocationsMasterID (ignores status)
- --dry-run: preview without writing to Notion
- CSV logging to logs/match_location_master_log.csv (UTF-8 with BOM)
"""

from __future__ import annotations
import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Project imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config
from scripts import notion_utils as nu

# Optional Google enrichment (safe if file not present)
try:
    from scripts.google_utils import place_details as google_place_details
except Exception:
    google_place_details = None  # safe fallback

# ─── Tunables ────────────────────────────────────────────────────────────────
PROXIMITY_METERS = 10.0
RATE_LIMIT_DELAY = 0.15  # seconds between writes
DEFAULT_STATUS_GEOCODED = "Geocoded"   # use your actual value if different

# Logging
LOGS_DIR = project_root / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOGS_DIR / "match_location_master_log.csv"

# ─── CSV logging ─────────────────────────────────────────────────────────────

def _log_row(row: Dict[str, Any]) -> None:
    exists = LOG_PATH.exists()
    # enforce header order
    fieldnames = [
        "timestamp",
        "production_db_name",
        "production_page_id",
        "full_address",
        "action",            # linked_by_place_id | linked_by_proximity | created_master | skipped | error
        "reason",
        "master_page_id",
        "master_id_title",   # LOC###
        "place_id",
        "distance_meters",
        "latitude",
        "longitude",
        "prodlocid",
        "dry_run"
    ]
    with open(LOG_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        # only send known keys; fill missing with ""
        sanitized = {k: row.get(k, "") for k in fieldnames}
        w.writerow(sanitized)

def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _load_table_map(path: Path) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _select_database_interactive(table_map: Dict[str, str]) -> Tuple[str, str]:
    names = list(table_map.keys())
    print("\nSelect a production-specific database:")
    for i, name in enumerate(names, 1):
        print(f"  [{i}] {name}")
    while True:
        choice = input("> ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            key = names[int(choice) - 1]
            return key, table_map[key]
        print("Invalid choice.")

def _rt_get(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("rich_text", [])
    return arr[0].get("plain_text", "") if arr else ""

def _title_get(props: Dict[str, Any], key: str) -> str:
    arr = props.get(key, {}).get("title", [])
    return arr[0].get("plain_text", "") if arr else ""

def _status_name(props: Dict[str, Any]) -> str:
    st = props.get("Status", {}).get("status", {})
    return st.get("name") or ""

def _get_num(props: Dict[str, Any], key: str) -> Optional[float]:
    v = props.get(key, {}).get("number")
    return None if v is None else float(v)

def _get_relation_ids(props: Dict[str, Any], key: str) -> List[str]:
    arr = props.get(key, {}).get("relation", [])
    return [x.get("id") for x in arr if isinstance(x, dict) and "id" in x]

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0  # meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def _find_master_by_place_id(master_id: str, place_id: str) -> Optional[str]:
    if not place_id:
        return None
    results = nu.query_database(
        master_id,
        filter_payload={"property": "Place_ID", "rich_text": {"equals": place_id}}
    )
    return results[0]["id"] if results else None

def _find_master_by_proximity(master_id: str, lat: float, lng: float) -> Tuple[Optional[str], float]:
    pages = nu.query_database(master_id)
    nearest_id, nearest_d = None, float("inf")
    for p in pages:
        props = p.get("properties", {})
        mlat = _get_num(props, "Latitude")
        mlng = _get_num(props, "Longitude")
        if mlat is None or mlng is None:
            continue
        d = _haversine_m(lat, lng, mlat, mlng)
        if d < nearest_d:
            nearest_id, nearest_d = p["id"], d
    if nearest_id and nearest_d <= PROXIMITY_METERS:
        return nearest_id, nearest_d
    return None, nearest_d

def _next_loc_master_id(master_id: str) -> str:
    pages = nu.query_database(master_id)
    max_num = 0
    for p in pages:
        title_arr = p["properties"].get("LocationsMasterID", {}).get("title", [])
        if not title_arr:
            continue
        text = title_arr[0]["text"]["content"]
        if isinstance(text, str) and text.startswith("LOC"):
            try:
                n = int(text[3:])
                max_num = max(max_num, n)
            except ValueError:
                pass
    return f"LOC{str(max_num + 1).zfill(3)}"

# ─── Core matching ───────────────────────────────────────────────────────────

def _process_page(
    production_db_name: str,
    production_db_id: str,
    master_db_id: str,
    page: Dict[str, Any],
    dry_run: bool
) -> None:
    props = page.get("properties", {})
    page_id = page.get("id", "")

    # Preconditions: needs a geocode + no existing link (by default; --all widens selection)
    addr = _rt_get(props, "Full Address")
    place_id = _rt_get(props, "Place_ID")
    lat = _get_num(props, "Latitude")
    lng = _get_num(props, "Longitude")
    prodlocid = _title_get(props, "ProdLocID")
    existing_link = _get_relation_ids(props, "LocationsMasterID")

    if not addr:
        msg = "empty address"
        print(f"  - Skipping {page_id}: {msg}")
        _log_row({
            "timestamp": _now_str(),
            "production_db_name": production_db_name,
            "production_page_id": page_id,
            "full_address": "",
            "action": "skipped",
            "reason": msg,
            "master_page_id": "",
            "master_id_title": "",
            "place_id": "",
            "distance_meters": "",
            "latitude": "",
            "longitude": "",
            "prodlocid": prodlocid,
            "dry_run": str(dry_run).lower(),
        })
        return

    if existing_link:
        msg = "already linked"
        print(f"  - Skipping {page_id}: {msg}")
        _log_row({
            "timestamp": _now_str(),
            "production_db_name": production_db_name,
            "production_page_id": page_id,
            "full_address": addr,
            "action": "skipped",
            "reason": msg,
            "master_page_id": existing_link[0],
            "master_id_title": "",
            "place_id": place_id,
            "distance_meters": "",
            "latitude": lat or "",
            "longitude": lng or "",
            "prodlocid": prodlocid,
            "dry_run": str(dry_run).lower(),
        })
        return

    if lat is None or lng is None:
        msg = "no lat/lng"
        print(f"  - Skipping {page_id}: {msg}")
        _log_row({
            "timestamp": _now_str(),
            "production_db_name": production_db_name,
            "production_page_id": page_id,
            "full_address": addr,
            "action": "skipped",
            "reason": msg,
            "master_page_id": "",
            "master_id_title": "",
            "place_id": place_id,
            "distance_meters": "",
            "latitude": "",
            "longitude": "",
            "prodlocid": prodlocid,
            "dry_run": str(dry_run).lower(),
        })
        return

    # 1) By Place_ID
    master_page_id = _find_master_by_place_id(master_db_id, place_id) if place_id else None
    if master_page_id:
        updates = {"LocationsMasterID": nu.format_relation(master_page_id)}
        if dry_run:
            print(f"[DRY-RUN] Would link {page_id} by Place_ID → {master_page_id}")
        else:
            nu.update_page(page_id, updates)
            time.sleep(RATE_LIMIT_DELAY)

        _log_row({
            "timestamp": _now_str(),
            "production_db_name": production_db_name,
            "production_page_id": page_id,
            "full_address": addr,
            "action": "linked_by_place_id",
            "reason": "",
            "master_page_id": master_page_id,
            "master_id_title": "",  # can be filled by a follow-up read if needed
            "place_id": place_id,
            "distance_meters": "",
            "latitude": lat,
            "longitude": lng,
            "prodlocid": prodlocid,
            "dry_run": str(dry_run).lower(),
        })
        return

    # 2) Proximity
    nearest_id, nearest_d = _find_master_by_proximity(master_db_id, lat, lng)
    if nearest_id:
        updates = {"LocationsMasterID": nu.format_relation(nearest_id)}
        if dry_run:
            print(f"[DRY-RUN] Would link {page_id} by proximity ({nearest_d:.1f} m) → {nearest_id}")
        else:
            nu.update_page(page_id, updates)
            time.sleep(RATE_LIMIT_DELAY)

        _log_row({
            "timestamp": _now_str(),
            "production_db_name": production_db_name,
            "production_page_id": page_id,
            "full_address": addr,
            "action": "linked_by_proximity",
            "reason": "",
            "master_page_id": nearest_id,
            "master_id_title": "",
            "place_id": place_id,
            "distance_meters": f"{nearest_d:.2f}",
            "latitude": lat,
            "longitude": lng,
            "prodlocid": prodlocid,
            "dry_run": str(dry_run).lower(),
        })
        return

    # 3) Create Master
    loc_title = _next_loc_master_id(master_db_id)

    # Optional Google enrichment (safe if helper missing)
    details = {}
    if place_id and google_place_details:
        try:
            details = google_place_details(place_id, ["name", "types", "url", "international_phone_number", "website"])
        except Exception:
            details = {}

    master_props = {
        "LocationsMasterID": nu.format_title(loc_title),
        "Place_ID": nu.format_rich_text(place_id or ""),
        "Latitude": nu.format_number(lat),
        "Longitude": nu.format_number(lng),
    }
    if details.get("name"):
        master_props["Name"] = nu.format_rich_text(details.get("name"))
    if details.get("types"):
        master_props["Types"] = nu.format_multi_select(details["types"][:10])
    if details.get("url"):
        master_props["Google Maps URL"] = nu.format_url(details["url"])
    if details.get("international_phone_number"):
        master_props["Phone"] = nu.format_phone_number(details["international_phone_number"])
    if details.get("website"):
        master_props["Website"] = nu.format_url(details["website"])

    if dry_run:
        print(f"[DRY-RUN] Would CREATE master {loc_title} and link production {page_id}")
        new_master_id = "DRY_RUN_MASTER"
    else:
        created = nu.create_page(master_db_id, master_props)
        new_master_id = created.get("id")
        time.sleep(RATE_LIMIT_DELAY)

    # Link production → master
    updates = {"LocationsMasterID": nu.format_relation(new_master_id)}
    if dry_run:
        print(f"[DRY-RUN] Would link {page_id} → {new_master_id}")
    else:
        nu.update_page(page_id, updates)
        time.sleep(RATE_LIMIT_DELAY)

    _log_row({
        "timestamp": _now_str(),
        "production_db_name": production_db_name,
        "production_page_id": page_id,
        "full_address": addr,
        "action": "created_master",
        "reason": "",
        "master_page_id": new_master_id,
        "master_id_title": loc_title,
        "place_id": place_id,
        "distance_meters": "",
        "latitude": lat,
        "longitude": lng,
        "prodlocid": prodlocid,
        "dry_run": str(dry_run).lower(),
    })

# ─── Query selection ─────────────────────────────────────────────────────────

def _query_candidates(prod_db_id: str, ready_status: str, include_all: bool) -> List[Dict[str, Any]]:
    """
    Default: rows where Status == ready_status (e.g., 'Geocoded') and LocationsMasterID empty.
    If include_all=True: any row with empty LocationsMasterID (ignore Status).
    """
    if include_all:
        return nu.query_database(
            prod_db_id,
            filter_payload={
                "property": "LocationsMasterID",
                "relation": {"is_empty": True}
            }
        )
    return nu.query_database(
        prod_db_id,
        filter_payload={
            "and": [
                {"property": "Status", "status": {"equals": ready_status}},
                {"property": "LocationsMasterID", "relation": {"is_empty": True}}
            ]
        }
    )

# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    load_dotenv(dotenv_path=project_root / ".env")

    if not Config.NOTION_TOKEN:
        print("❌ NOTION_TOKEN missing in .env.")
        return
    if not Config.LOCATIONS_MASTER_DB:
        print("❌ LOCATIONS_MASTER_DB missing in .env.")
        return

    parser = argparse.ArgumentParser(description="Match geocoded production rows to Locations Master.")
    parser.add_argument("--db-name", help="Name from notion_tables.json (e.g., TGD_Locations). If omitted, interactive selection.")
    parser.add_argument("--all", action="store_true", help="Process all rows with empty LocationsMasterID (ignore Status).")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; no writes to Notion.")
    args = parser.parse_args()

    table_map_path = project_root / "notion_tables.json"
    try:
        table_map = _load_table_map(table_map_path)
    except FileNotFoundError:
        print("❌ notion_tables.json not found. Run Sync Production Tables first.")
        return

    if args.db_name:
        if args.db_name not in table_map:
            print(f"❌ db-name '{args.db_name}' not found in notion_tables.json.")
            return
        prod_name, prod_db_id = args.db_name, table_map[args.db_name]
    else:
        prod_name, prod_db_id = _select_database_interactive(table_map)

    ready_status = DEFAULT_STATUS_GEOCODED  # adjust if you store this elsewhere
    print(f"\nUsing production DB: {prod_name}")
    print(f"Selection: {'ALL rows with empty link' if args.all else f'Status == {ready_status!r} and empty link'}")
    candidates = _query_candidates(prod_db_id=prod_db_id, ready_status=ready_status, include_all=args.all)

    if not candidates:
        print("No matching rows to process.")
        return

    print(f"Found {len(candidates)} row(s) to match.")
    for page in candidates:
        try:
            _process_page(
                production_db_name=prod_name,
                production_db_id=prod_db_id,
                master_db_id=Config.LOCATIONS_MASTER_DB,
                page=page,
                dry_run=args.dry_run
            )
        except Exception as e:
            print(f"  - ❌ Error processing {page.get('id')}: {e}")
            _log_row({
                "timestamp": _now_str(),
                "production_db_name": prod_name,
                "production_page_id": page.get("id", ""),
                "full_address": _rt_get(page.get("properties", {}), "Full Address"),
                "action": "error",
                "reason": str(e),
                "master_page_id": "",
                "master_id_title": "",
                "place_id": _rt_get(page.get("properties", {}), "Place_ID"),
                "distance_meters": "",
                "latitude": _get_num(page.get("properties", {}), "Latitude") or "",
                "longitude": _get_num(page.get("properties", {}), "Longitude") or "",
                "prodlocid": _title_get(page.get("properties", {}), "ProdLocID"),
                "dry_run": str(args.dry_run).lower(),
            })

    print(f"\n✅ Done. Log: {LOG_PATH}")

if __name__ == "__main__":
    main()
