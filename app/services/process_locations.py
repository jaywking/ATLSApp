import json
from pathlib import Path
from typing import Dict, List, Optional

import logging
import traceback

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services._runner import run_script
from app.services.preflight import run_preflight
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['process'])

_TABLE_MAP_PATH = Path(__file__).resolve().parent.parent.parent / 'notion_tables.json'
logger = logging.getLogger(__name__)


class TableRequest(BaseModel):
    table_key: Optional[str] = None


def _load_table_map() -> Dict[str, str]:
    try:
        with _TABLE_MAP_PATH.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail='notion_tables.json not found. Run sync_prod_tables first.',
        ) from exc

    if not isinstance(data, dict) or not data:
        raise HTTPException(
            status_code=400,
            detail='notion_tables.json is empty or malformed.',
        )
    return data


def _resolve_selection(table_map: Dict[str, str], requested_key: Optional[str]) -> str:
    keys: List[str] = list(table_map.keys())
    if requested_key and requested_key not in table_map:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown production '{requested_key}'.",
        )
    if not keys:
        raise HTTPException(
            status_code=400,
            detail='No productions available in notion_tables.json.',
        )
    return requested_key or keys[0]


@router.post('/process', response_model=ScriptResponse)
async def process_locations(payload: TableRequest) -> ScriptResponse:
    try:
        issues = run_preflight(check_tables=True)
        if issues:
            raise HTTPException(status_code=400, detail='; '.join(issues))

        table_map = _load_table_map()
        selected_key = _resolve_selection(table_map, payload.table_key)
        selection_index = list(table_map.keys()).index(selected_key) + 1
        return await run_script(
            'process_new_locations.py',
            input_data=f'{selection_index}\n',
        )
    except HTTPException:
        raise
    except FileNotFoundError:
        return ScriptResponse(
            success=False,
            returncode=127,
            stderr='process_new_locations.py not found in scripts/.',
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.error('Process locations failed: %s\n%s', exc, traceback.format_exc())
        return ScriptResponse(
            success=False,
            returncode=1,
            stderr=f'{type(exc).__name__}: {exc}' or 'Unexpected error.',
        )
