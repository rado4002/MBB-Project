"""Administrator browser contracts for Operator account management."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.models.operator_account import (
    normalize_display_name,
    normalize_email,
    normalize_username,
)


class OperatorAccountSummary(BaseModel):
    account_id: UUID
    username: str
    display_name: str
    email: str | None
    status: Literal["active", "disabled"]
    last_login_at: datetime | None
    created_at: datetime


class OperatorAccountListResponse(BaseModel):
    items: list[OperatorAccountSummary]


class OperatorAccountCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def _username_is_valid(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_is_valid(cls, value: str) -> str:
        return normalize_display_name(value)

    @field_validator("email")
    @classmethod
    def _email_is_valid(cls, value: str | None) -> str | None:
        return normalize_email(value)


class OperatorPasswordSet(BaseModel):
    new_password: SecretStr
