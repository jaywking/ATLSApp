from fastapi import APIRouter

from app.services._runner import run_script
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['lha'])


@router.post('/lha', response_model=ScriptResponse)
async def generate_lha() -> ScriptResponse:
    try:
        return await run_script('generate_lha_forms.py')
    except FileNotFoundError:
        return ScriptResponse(
            success=False,
            returncode=127,
            stderr='generate_lha_forms.py not found in scripts/.',
        )
    except Exception as exc:  # pragma: no cover
        return ScriptResponse(
            success=False,
            returncode=1,
            stderr=f'{type(exc).__name__}: {exc}' or 'Unexpected error.',
        )
