from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from agent_eval.db.enums import SessionRole


class LoginRequest(BaseModel):
    role: SessionRole
    password: str = Field(min_length=1, max_length=1024)


class UserLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    role: SessionRole
    expires_at: datetime
    user_id: UUID
    display_name: str
    is_shared: bool
    workspace_enabled: bool
