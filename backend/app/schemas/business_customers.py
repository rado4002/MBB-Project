"""Browser Customer profile and contact-preference contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import Language


class CustomerChannelEvidence(BaseModel):
    channel: Literal["whatsapp"]
    provenance: Literal["verified_provider_message"]


class BusinessCustomerListItem(BaseModel):
    customer_id: str
    customer_reference: UUID
    conversation_id: UUID | None = None
    display_name: str | None = None
    phone_number: str
    communication_channel: Literal["whatsapp"] | None = None
    acquisition_source: str | None = Field(default=None, max_length=50)
    acquisition_source_provenance: Literal["earliest_recorded_lead"] | None = None
    last_interaction_at: datetime


class BusinessCustomerListResponse(BaseModel):
    items: list[BusinessCustomerListItem] = Field(max_length=25)
    has_more: bool


class CustomerOptOutEvidence(BaseModel):
    message_id: UUID
    received_at: datetime
    content_excerpt: str = Field(max_length=120)
    content_truncated: bool
    provenance: Literal["inbound_whatsapp_customer_message"]


class BusinessCustomerProfile(BaseModel):
    customer_id: str
    conversation_id: UUID | None = None
    display_name: str | None = None
    phone_number: str
    city: str
    preferred_language: Language
    opted_out: bool
    opted_out_at: datetime | None = None
    consent_given: bool
    consent_timestamp: datetime | None = None
    communication_channels: list[CustomerChannelEvidence] = Field(max_length=1)
    acquisition_sources: list[str] = Field(max_length=5)
    opt_out_evidence: list[CustomerOptOutEvidence] = Field(max_length=5)


class CustomerProfileSnapshot(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    city: str = Field(min_length=1, max_length=50)
    preferred_language: Language


class CustomerProfileInput(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    city: str = Field(min_length=1, max_length=50)
    preferred_language: Language

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("city must not be blank")
        return normalized


class CustomerProfileUpdate(BaseModel):
    expected: CustomerProfileSnapshot
    profile: CustomerProfileInput
    reason_code: Literal["customer_provided_update", "operator_correction"]


class CustomerOptOutRequest(BaseModel):
    expected_opted_out: Literal[False]
    evidence_message_id: UUID
    attested: Literal[True]
