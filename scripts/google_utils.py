from __future__ import annotations
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

import requests

# Project imports
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config

# ─── GOOGLE API HELPERS ───────────────────────────────────────────────────────

def _make_google_request(url: str, params: Dict, api_key: str, max_retries: int = 3, backoff_factor: float = 0.5) -> Dict:
    """Makes a request to a Google API with retry logic."""
    params['key'] = api_key
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("status") in ["OK", "ZERO_RESULTS"]:
                return data
            else:
                print(f"  - Google API Error: {data.get('status')}: {data.get('error_message')}")
                # Don't retry on specific non-transient errors
                if data.get("status") in ["REQUEST_DENIED", "INVALID_REQUEST"]:
                    return data
        except requests.exceptions.RequestException as e:
            print(f"  - Request failed (attempt {attempt + 1}/{max_retries}): {e}")
        
        time.sleep(backoff_factor * (2 ** attempt))
    
    print("  - Max retries reached. Google API request failed.")
    return {"status": "MAX_RETRIES_EXCEEDED"}

def _normalize_formatted_address(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    text = value.strip()
    if not text:
        return text
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r",\s*(USA|United States)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*$", "", text)
    return text.strip()


def geocode(address: str) -> Optional[Dict[str, Any]]:
    """Geocodes an address string to lat/lng and place_id."""
    if not Config.GOOGLE_MAPS_API_KEY:
        print("  - Skipping geocode: GOOGLE_MAPS_API_KEY not set.")
        return None
    
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address}
    data = _make_google_request(url, params, Config.GOOGLE_MAPS_API_KEY)
    
    if data and data.get("status") == "OK":
        result = data["results"][0]
        return {
            "lat": result["geometry"]["location"]["lat"],
            "lng": result["geometry"]["location"]["lng"],
            "place_id": result.get("place_id"),
            "formatted_address": _normalize_formatted_address(result.get("formatted_address")),
        }
    return None

def place_details(place_id: str, fields: List[str]) -> Dict[str, Any]:
    """Fetches specific details for a Google Place ID."""
    if not Config.GOOGLE_MAPS_API_KEY:
        print("  - Skipping place details: GOOGLE_MAPS_API_KEY not set.")
        return {}
        
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": ",".join(fields),
    }
    data = _make_google_request(url, params, Config.GOOGLE_MAPS_API_KEY)
    
    if data and data.get("status") == "OK":
        return data.get("result", {})
    return {}

def nearby_places(
    latitude: float,
    longitude: float,
    radius: Optional[int] = None,
    place_type: Optional[str] = None,
    keyword: Optional[str] = None,
    rankby: Optional[str] = None,
    max_pages: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Searches for places near a given lat/lng.
    """
    if not Config.GOOGLE_MAPS_API_KEY:
        print("  - Skipping nearby search: GOOGLE_MAPS_API_KEY not set.")
        return []

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    all_results = []
    pages_fetched = 0
    params = {
        "location": f"{latitude},{longitude}",
    }
    if keyword:
        params["keyword"] = keyword
    if place_type:
        params["type"] = place_type
    
    if rankby:
        params["rankby"] = rankby
    elif radius:
        params["radius"] = radius

    while True:
        if max_pages is not None and pages_fetched >= max_pages:
            break

        data = _make_google_request(url, params, Config.GOOGLE_MAPS_API_KEY)
        pages_fetched += 1
        
        if data and data.get("status") == "OK":
            all_results.extend(data.get("results", []))
            next_page_token = data.get("next_page_token")

            if next_page_token:
                params = {"pagetoken": next_page_token}
                time.sleep(2)  # Google requires a short delay before fetching the next page
            else:
                break
        else:
            break

    return all_results
