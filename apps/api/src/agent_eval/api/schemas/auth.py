from datetime import datetime

from pydantic import BaseModel, Field

from agent_eval.db.enums import SessionRole


class LoginRequest(BaseModel):
    role: SessionRole
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    role: SessionRole
    expires_at: datetime
