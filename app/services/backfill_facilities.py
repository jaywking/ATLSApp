from fastapi import APIRouter, HTTPException

from app.services._runner import run_script
from app.services.preflight import run_preflight
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['backfill'])


@router.post('/backfill', response_model=ScriptResponse)
async def backfill_facilities() -> ScriptResponse:
    try:
        issues = run_preflight(check_tables=True)
        if issues:
            raise HTTPException(status_code=400, detail='; '.join(issues))

        return await run_script(
            'fetch_medical_facilities.py',
            args=('--dry-run', '--backfill-existing'),
        )
    except FileNotFoundError:
        return ScriptResponse(
            success=False,
            returncode=127,
            stderr='fetch_medical_facilities.py not found in scripts/.',
        )
    except Exception as exc:  # pragma: no cover
        return ScriptResponse(
            success=False,
            returncode=1,
            stderr=f'{type(exc).__name__}: {exc}' or 'Unexpected error.',
        )
