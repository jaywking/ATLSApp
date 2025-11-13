from fastapi import APIRouter, HTTPException

from app.services._runner import run_script
from app.services.preflight import run_preflight
from app.services.process_locations import TableRequest, _load_table_map, _resolve_selection
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['reprocess'])


@router.post('/reprocess', response_model=ScriptResponse)
async def reprocess_locations(payload: TableRequest) -> ScriptResponse:
    try:
        issues = run_preflight(check_tables=True)
        if issues:
            raise HTTPException(status_code=400, detail='; '.join(issues))

        table_map = _load_table_map()
        selected_key = _resolve_selection(table_map, payload.table_key)
        selection_index = list(table_map.keys()).index(selected_key) + 1
        return await run_script(
            'process_new_locations.py',
            args=('--all',),
            input_data=f'{selection_index}\nn\n',
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
        return ScriptResponse(
            success=False,
            returncode=1,
            stderr=f'{type(exc).__name__}: {exc}' or 'Unexpected error.',
        )
