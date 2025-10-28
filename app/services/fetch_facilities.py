from fastapi import APIRouter

from app.services._runner import run_script
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['facilities'])


@router.post('/facilities', response_model=ScriptResponse)
async def fetch_facilities() -> ScriptResponse:
    try:
        return await run_script('fetch_medical_facilities.py')
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
