"""Schemas for EP-20–EP-22 (M4 Customer / Opt-Out) and A-series customer admin."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import Language


class CustomerResponse(BaseModel):
    phone_number: str
    name: str | None = None
    city: str | None = None
    language: Language | None = None
    club_member: bool = False
    opt_out: bool = False
    created_at: datetime
    updated_at: datetime


class OptOutRequest(BaseModel):
    phone_number: str = Field(..., description="DRC E.164: +243XXXXXXXXX")
    reason: str | None = Field(None, max_length=200)


class OptOutResponse(BaseModel):
    phone_number: str
    opt_out: bool
    opted_out_at: datetime


class CustomerLanguageUpdate(BaseModel):
    language: Language


class CustomerLanguageResponse(BaseModel):
    phone_number: str
    language: Language
    updated_at: datetime
