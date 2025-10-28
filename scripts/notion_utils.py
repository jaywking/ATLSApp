import os
import time
import random
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional, List

import requests

# Add project root to path and import central config
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))
from config import Config

# ─── CORE API FUNCTIONS ─────────────────────────────────────────────────────

def _make_request(method: str, url: str, max_retries: int = 3, backoff_factor: float = 0.5, **kwargs: Any) -> requests.Response:
    """
    A wrapper for requests that includes automatic retries on transient errors.
    """
    # Generate headers dynamically at request time to ensure Config is loaded.
    headers = {
        "Authorization": f"Bearer {Config.NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    # Allow for overriding or adding headers from the function call
    headers.update(kwargs.get("headers", {}))
    kwargs["headers"] = headers
    kwargs.setdefault("timeout", 15) # Add a timeout

    for attempt in range(max_retries):
        try:
            res = requests.request(method, url, **kwargs)
            res.raise_for_status()  # Raises HTTPError for 4xx/5xx responses
            return res
        except requests.exceptions.RequestException as e:
            # Retry on server errors (5xx) and timeouts/connection errors
            is_server_error = hasattr(e, 'response') and e.response is not None and 500 <= e.response.status_code < 600
            is_timeout = isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))

            if is_server_error or is_timeout:
                if attempt + 1 == max_retries:
                    logging.error(f"Max retries reached for {method} {url}. Last error: {e}")
                    raise
                sleep_time = backoff_factor * (2 ** attempt) + random.uniform(0, 1)
                logging.warning(f"Network error ({e}). Retrying in {sleep_time:.2f}s... ({attempt + 1}/{max_retries})")
                time.sleep(sleep_time)
            else:
                # For client errors (4xx), print the detailed message from Notion's response
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        # This will print the specific validation error, e.g., "Status property should be one of..."
                        logging.error(f"Notion API Error: {e.response.json().get('message')}")
                    except (json.JSONDecodeError, AttributeError): # If the error response isn't the expected JSON
                        logging.error(f"Notion API Error: {e.response.status_code} {e.response.reason}. Response: {e.response.text}")
                    logging.error(f"Request body: {json.dumps(kwargs.get('json')) if kwargs.get('json') else 'None'}")
                raise # Re-raise non-retryable client errors
    raise RuntimeError("Request failed after all retries.") # Should be unreachable

def query_database(database_id: str, filter_payload: Optional[dict] = None) -> list[dict]:
    """
    Queries a Notion database, handling pagination automatically.
    Returns a list of all page results.
    """
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    all_results = []
    next_cursor = None
    while True:
        payload = {"page_size": 100}
        if filter_payload:
            payload["filter"] = filter_payload
        if next_cursor:
            payload["start_cursor"] = next_cursor

        try:
            res = _make_request("POST", url, json=payload)
            data = res.json()
            all_results.extend(data.get("results", []))
            if not data.get("has_more"):
                return all_results
            next_cursor = data.get("next_cursor")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error querying database {database_id}: {e}")
            return all_results # Return what we have so far

def get_page(page_id: str) -> dict:
    """Retrieves a single Notion page."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = _make_request("GET", url)
    return res.json()

def get_database(database_id: str) -> dict:
    """Retrieves a database object."""
    url = f"https://api.notion.com/v1/databases/{database_id}"
    res = _make_request("GET", url)
    return res.json()

def update_page(page_id: str, properties_payload: dict) -> dict:
    """Updates a Notion page with the given properties."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = _make_request("PATCH", url, json={"properties": properties_payload})
    return res.json()

def update_database(database_id: str, properties_payload: dict) -> dict:
    """Updates a database's properties (e.g., adding a select option)."""
    url = f"https://api.notion.com/v1/databases/{database_id}"
    res = _make_request("PATCH", url, json=properties_payload)
    return res.json()

def create_database(parent_page_id: str, title: list, schema: dict, is_inline: bool = False) -> dict:
    """Creates a new database as a sub-page of a given page."""
    url = "https://api.notion.com/v1/databases"
    payload = {"parent": {"page_id": parent_page_id}, "title": title, "properties": schema, "is_inline": is_inline}
    res = _make_request("POST", url, json=payload)
    return res.json()

def create_page(parent_db_id: str, properties_payload: dict, extra_payload: Optional[dict] = None) -> dict:
    """Creates a new page in the specified database."""
    url = "https://api.notion.com/v1/pages"
    if parent_db_id:
        # Creating a page inside a database
        payload = {"parent": {"database_id": parent_db_id}, "properties": properties_payload}
    else:
        # Creating a database (which is a type of page)
        # The properties_payload is the full schema, not just the values.
        payload = {"properties": properties_payload} # This is the schema definition
    if extra_payload:
        payload.update(extra_payload)
        # If creating a database, the schema is passed in extra_payload, so we replace the 'properties' key.
        if not parent_db_id and "properties" in extra_payload:
            payload["properties"] = extra_payload.pop("properties")

    res = _make_request("POST", url, json=payload)
    return res.json()

def archive_page(page_id: str) -> dict:
    """Archives (deletes) a Notion page."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = _make_request("PATCH", url, json={"archived": True})
    return res.json()

# ─── FORMATTING HELPERS ─────────────────────────────────────────────────────

def format_rich_text(value: Any) -> dict: return {"rich_text": [{"text": {"content": str(value)}}]}
def format_number(value: float | int) -> dict: return {"number": value}
def format_status(value: str) -> dict: return {"status": {"name": value}}
def format_title(value: Any) -> dict: return {"title": [{"text": {"content": str(value)}}]}
def format_url(value: str) -> dict: return {"url": value}
def format_phone_number(value: str) -> dict: return {"phone_number": value}
def format_relation(ids: str | List[str]) -> dict:
    id_list = [ids] if isinstance(ids, str) else ids
    return {"relation": [{"id": i} for i in id_list]}
def format_multi_select(values: List[str]) -> dict: return {"multi_select": [{"name": v} for v in values]}
def format_select(value: Any) -> dict: return {"select": {"name": str(value)}}