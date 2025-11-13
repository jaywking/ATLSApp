import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence

from app.services.schemas import ScriptResponse

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _ROOT_DIR / 'logs'
_LOG_DIR.mkdir(exist_ok=True)
_RUNNER_LOG = _LOG_DIR / 'jobs.log'

logger = logging.getLogger('services.runner')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.FileHandler(_RUNNER_LOG, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


async def run_script(
    script_name: str,
    args: Sequence[str] = (),
    *,
    input_data: Optional[str] = None,
    env_overrides: Optional[Mapping[str, str]] = None,
) -> ScriptResponse:
    script_path = _ROOT_DIR / 'scripts' / script_name
    if not script_path.exists():
        raise FileNotFoundError(script_path)

    module_name = f'scripts.{script_path.stem}'
    start_time = datetime.utcnow()

    env = os.environ.copy()
    env.setdefault('PYTHONUTF8', '1')
    if env_overrides:
        env.update(env_overrides)

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            module_name,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            env=env,
        )

        stdout_bytes, stderr_bytes = await process.communicate(
            input_data.encode('utf-8') if input_data is not None else None
        )
        returncode = process.returncode
    except NotImplementedError:
        def _run_sync() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [sys.executable, '-m', module_name, *args],
                input=input_data.encode('utf-8') if input_data is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

        completed = await asyncio.to_thread(_run_sync)
        stdout_bytes = completed.stdout
        stderr_bytes = completed.stderr
        returncode = completed.returncode

    stdout_text = stdout_bytes.decode('utf-8', errors='replace')
    stderr_text = stderr_bytes.decode('utf-8', errors='replace')

    response = ScriptResponse(
        success=returncode == 0,
        returncode=returncode,
        stdout=stdout_text.strip() or None,
        stderr=stderr_text.strip() or None,
    )

    logger.info(
        'script=%s args=%s returncode=%s duration_seconds=%.2f stdout=%s stderr=%s',
        module_name,
        ' '.join(args),
        response.returncode,
        (datetime.utcnow() - start_time).total_seconds(),
        (response.stdout or '').replace('\n', '\\n')[:2000],
        (response.stderr or '').replace('\n', '\\n')[:2000],
    )

    return response
