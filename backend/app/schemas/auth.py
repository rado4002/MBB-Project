"""Browser-authentication request and response contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class BrowserErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    retry_after_seconds: int | None = None


class BrowserErrorEnvelope(BaseModel):
    error: BrowserErrorDetail


class CsrfResponse(BaseModel):
    csrf_token: str
    expires_at_epoch: int


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr


class PasswordRequest(BaseModel):
    password: SecretStr


class PasswordChangeRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr


class HumanSummary(BaseModel):
    account_id: str
    username: str
    display_name: str
    role: Literal["administrator", "operator", "analyst"]


class BrowserSessionResponse(BaseModel):
    human: HumanSummary
    capabilities: list[str]
    must_change_password: bool
    idle_expires_at_epoch: int
    absolute_expires_at_epoch: int
    recent_reauthentication_expires_at_epoch: int | None
    csrf_token: str | None = None


class LogoutResponse(BaseModel):
    logged_out: bool = True
