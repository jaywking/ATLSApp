import asyncio
import json
import os
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI
from nicegui import context, ui
from dotenv import load_dotenv

from app.services.backfill_facilities import router as backfill_router
from app.services.fetch_facilities import router as facilities_router
from app.services.generate_lha import router as lha_router
from app.services.process_locations import router as process_router
from app.services.reprocess_locations import router as reprocess_router

# Load .env from project root
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(env_path)

NOTION_TOKEN = os.getenv('NOTION_TOKEN')
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

app = FastAPI(title='ATLSApp')
app.include_router(process_router)
app.include_router(reprocess_router)
app.include_router(facilities_router)
app.include_router(backfill_router)
app.include_router(lha_router)

_client_origins: Dict[int, str] = {}
_origin_lock = asyncio.Lock()


async def _get_client_origin() -> str:
  client = context.client
  if client.id in _client_origins:
    return _client_origins[client.id]

  async with _origin_lock:
    if client.id in _client_origins:
      return _client_origins[client.id]

    origin = await ui.run_javascript('return window.location.origin')
    _client_origins[client.id] = origin
    return origin


async def _call_api(
  path: str,
  *,
  method: str = 'POST',
  json: Optional[Dict[str, Any]] = None,
  timeout: float = 300,
) -> requests.Response:
  origin = await _get_client_origin()
  url = f'{origin}{path}'
  request_fn = partial(requests.request, method, url, json=json, timeout=timeout)
  return await asyncio.to_thread(request_fn)


async def _execute_job(
  *,
  path: str,
  status_label,
  log_area,
  running_status: str,
  start_toast: str,
  success_toast: str,
  default_success_detail: str,
  failure_toast: str,
  json_payload: Optional[Dict[str, Any]] = None,
) -> None:
  status_label.text = running_status
  ui.notify(start_toast, type='warning')
  log_area.push(f'Started: {start_toast}')

  try:
    response = await _call_api(path, json=json_payload)

    try:
      payload = response.json()
    except ValueError:
      payload = {}

    print(f'API call to {path} -> status={response.status_code}, payload={payload}')

    if response.ok and payload.get('success'):
      message = payload.get('stdout') or default_success_detail
      status_label.text = message
      log_area.push(f'Success: {message}')
      ui.notify(success_toast, type='positive')
    else:
      detail = (
        payload.get('detail')
        or payload.get('stderr')
        or payload.get('stdout')
        or response.text
      )
      status_label.text = detail
      log_area.push(f'Failure: {detail}')
      ui.notify(failure_toast, type='negative')
  except Exception as exc:  # pragma: no cover - guardrail
    status_label.text = str(exc)
    log_area.push(f'Error: {exc}')
    ui.notify(f'{failure_toast}: {exc}', type='negative')


@lru_cache(maxsize=1)
def _load_production_options() -> List[str]:
  data_path = Path(__file__).resolve().parent.parent / 'notion_tables.json'
  try:
    with data_path.open('r', encoding='utf-8') as handle:
      data = json.load(handle)
    if isinstance(data, dict):
      return list(data.keys())
  except FileNotFoundError:
    return []
  return []

# Sidebar layout and placeholder pages
@ui.page('/')
def index_page():
  with ui.left_drawer().classes('w-64'):
    ui.link('Process Locations', '/process')
    ui.link('Reprocess', '/reprocess')
    ui.link('Fetch Facilities', '/facilities')
    ui.link('Backfill Facilities', '/backfill')
    ui.link('Generate LHA', '/lha')
    ui.link('Logs', '/logs')

  with ui.column().classes('p-6 gap-2'):
    ui.label('ATLSApp Control Surface')
    ui.label(f'Notion token loaded: {"Yes" if NOTION_TOKEN else "No"}')
    ui.label(f'Google Maps key loaded: {"Yes" if GOOGLE_MAPS_API_KEY else "No"}')

@ui.page('/process')
def process_page():
  ui.label('Process Locations')

  with ui.card().classes('w-full max-w-xl gap-2'):
    status_label = ui.label('Idle').classes('text-sm text-gray-500 whitespace-pre-wrap')
    log_area = ui.log(max_lines=50).classes('w-full max-h-48')
    options = _load_production_options()
    table_select = ui.select(
      options,
      value=options[0] if options else None,
      label='Production',
    ).classes('w-full').props('outlined') if options else None

  async def trigger_process() -> None:
    if table_select is None or not table_select.value:
      status_label.text = 'No productions available.'
      log_area.push('Failure: No productions available.')
      ui.notify('No productions available to process', type='negative')
      return

    await _execute_job(
      path='/api/process',
      status_label=status_label,
      log_area=log_area,
      running_status='Processing locations...',
      start_toast='Processing started...',
      success_toast='Process locations completed',
      default_success_detail='Process completed successfully.',
      failure_toast='Process locations failed',
      json_payload={'table_key': table_select.value},
    )

  ui.button('Run', on_click=trigger_process)

@ui.page('/reprocess')
def reprocess_page():
  ui.label('Reprocess Locations')

  with ui.card().classes('w-full max-w-xl gap-2'):
    status_label = ui.label('Idle').classes('text-sm text-gray-500 whitespace-pre-wrap')
    log_area = ui.log(max_lines=50).classes('w-full max-h-48')
    options = _load_production_options()
    table_select = ui.select(
      options,
      value=options[0] if options else None,
      label='Production',
    ).classes('w-full').props('outlined') if options else None

  async def trigger_reprocess() -> None:
    if table_select is None or not table_select.value:
      status_label.text = 'No productions available.'
      log_area.push('Failure: No productions available.')
      ui.notify('No productions available to reprocess', type='negative')
      return

    await _execute_job(
      path='/api/reprocess',
      status_label=status_label,
      log_area=log_area,
      running_status='Reprocessing all locations...',
      start_toast='Reprocess started...',
      success_toast='Reprocess completed',
      default_success_detail='Reprocess completed successfully.',
      failure_toast='Reprocess failed',
      json_payload={'table_key': table_select.value},
    )

  ui.button('Run', on_click=trigger_reprocess)

@ui.page('/facilities')
def facilities_page():
  ui.label('Fetch Nearby Medical Facilities')

  with ui.card().classes('w-full max-w-xl gap-2'):
    status_label = ui.label('Idle').classes('text-sm text-gray-500 whitespace-pre-wrap')
    log_area = ui.log(max_lines=50).classes('w-full max-h-48')

  async def trigger_facilities() -> None:
    await _execute_job(
      path='/api/facilities',
      status_label=status_label,
      log_area=log_area,
      running_status='Fetching nearby medical facilities...',
      start_toast='Facility fetch started...',
      success_toast='Facility fetch completed',
      default_success_detail='Facility fetch completed successfully.',
      failure_toast='Facility fetch failed',
    )

  ui.button('Run', on_click=trigger_facilities)

@ui.page('/backfill')
def backfill_page():
  ui.label('Backfill Facilities')

  with ui.card().classes('w-full max-w-xl gap-2'):
    status_label = ui.label('Idle').classes('text-sm text-gray-500 whitespace-pre-wrap')
    log_area = ui.log(max_lines=50).classes('w-full max-h-48')

  async def trigger_backfill() -> None:
    await _execute_job(
      path='/api/backfill',
      status_label=status_label,
      log_area=log_area,
      running_status='Backfilling facility details...',
      start_toast='Backfill started...',
      success_toast='Backfill completed',
      default_success_detail='Backfill completed successfully.',
      failure_toast='Backfill failed',
    )

  ui.button('Run', on_click=trigger_backfill)

@ui.page('/lha')
def lha_page():
  ui.label('Generate LHA')

  with ui.card().classes('w-full max-w-xl gap-2'):
    status_label = ui.label('Idle').classes('text-sm text-gray-500 whitespace-pre-wrap')
    log_area = ui.log(max_lines=50).classes('w-full max-h-48')

  async def trigger_lha() -> None:
    await _execute_job(
      path='/api/lha',
      status_label=status_label,
      log_area=log_area,
      running_status='Generating LHA forms...',
      start_toast='LHA generation started...',
      success_toast='LHA generation completed',
      default_success_detail='LHA generation completed successfully.',
      failure_toast='LHA generation failed',
    )

  ui.button('Run', on_click=trigger_lha)

@ui.page('/logs')
def logs_page():
  ui.label('Logs')
  ui.label('Check /logs folder for CSVs and output')

ui.run_with(app)
