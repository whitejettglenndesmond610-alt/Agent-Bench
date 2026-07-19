from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkspacePromptTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=200_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name cannot be blank")
        return name

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value


class WorkspacePromptTemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name cannot be blank")
        return name

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("content cannot be blank")
        return value

    @model_validator(mode="after")
    def has_change(self) -> Self:
        changes = self.model_fields_set - {"expected_revision"}
        if not changes:
            raise ValueError("a name or content change is required")
        if any(getattr(self, field) is None for field in changes):
            raise ValueError("template fields cannot be null")
        return self


class WorkspacePromptTemplateDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class WorkspacePromptTemplateResponse(BaseModel):
    id: UUID
    name: str
    content: str
    revision: int
    created_at: datetime
    updated_at: datetime


class WorkspacePromptTemplateList(BaseModel):
    items: list[WorkspacePromptTemplateResponse]
