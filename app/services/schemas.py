from typing import Optional

from pydantic import BaseModel


class ScriptResponse(BaseModel):
    success: bool
    returncode: int
    stdout: Optional[str] = None
    stderr: Optional[str] = None
