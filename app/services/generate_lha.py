from fastapi import APIRouter, HTTPException

from app.services._runner import run_script
from app.services.preflight import run_preflight
from app.services.schemas import ScriptResponse

router = APIRouter(prefix='/api', tags=['lha'])


@router.post('/lha', response_model=ScriptResponse)
async def generate_lha() -> ScriptResponse:
    try:
        issues = run_preflight(check_tables=True)
        if issues:
            raise HTTPException(status_code=400, detail='; '.join(issues))

        response = await run_script('generate_lha_forms.py', input_data='m\n')
        if not response.success and response.stderr and 'EOFError' in response.stderr:
            response.stderr = (
                'LHA generation tool requires interactive selections; '
                'run scripts/generate_lha_forms.py manually for now.'
            )
        return response
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
