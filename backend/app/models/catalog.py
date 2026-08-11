"""Authoritative Catalog persistence for product families and sellable items."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base

MAX_ATTRIBUTE_KEYS = 20
MAX_ATTRIBUTE_KEY_LENGTH = 40
MAX_ATTRIBUTE_TEXT_LENGTH = 200
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")


def normalize_required_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    return normalized


def normalize_category_code(value: str) -> str:
    normalized = value.strip().lower()
    if not _CATEGORY_RE.fullmatch(normalized):
        raise ValueError("category_code must be normalized lower snake case")
    return normalized


def normalize_optional_label(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 100:
        raise ValueError("model_label must not exceed 100 characters")
    return normalized


def normalize_sku(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized:
        return None
    if not _SKU_RE.fullmatch(normalized):
        raise ValueError("sku has an invalid format")
    return normalized


def validate_sellable_attributes(value: Any) -> dict[str, str | int | bool]:
    if not isinstance(value, dict):
        raise ValueError("attributes must be an object")
    if len(value) > MAX_ATTRIBUTE_KEYS:
        raise ValueError(f"attributes must contain at most {MAX_ATTRIBUTE_KEYS} keys")
    validated: dict[str, str | int | bool] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("attribute keys must be strings")
        key = raw_key.strip().lower()
        if len(key) > MAX_ATTRIBUTE_KEY_LENGTH or not _ATTRIBUTE_KEY_RE.fullmatch(key):
            raise ValueError("attribute keys must be normalized lower snake case")
        if isinstance(raw_value, bool):
            validated[key] = raw_value
        elif isinstance(raw_value, int):
            validated[key] = raw_value
        elif isinstance(raw_value, str):
            text_value = raw_value.strip()
            if not text_value or len(text_value) > MAX_ATTRIBUTE_TEXT_LENGTH:
                raise ValueError(
                    "attribute text values must contain between 1 and "
                    f"{MAX_ATTRIBUTE_TEXT_LENGTH} characters"
                )
            validated[key] = text_value
        else:
            raise ValueError("attribute values must be strings, integers, or booleans")
    return validated


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 200",
            name="chk_products_name_length",
        ),
        CheckConstraint(
            "category_code ~ '^[a-z][a-z0-9_]{0,49}$'",
            name="chk_products_category_code",
        ),
        CheckConstraint(
            "char_length(btrim(description)) BETWEEN 1 AND 4000",
            name="chk_products_description_length",
        ),
        Index("idx_products_active_category", "active", "category_code"),
        Index("idx_products_name", "name"),
        {"schema": "mbb"},
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    @validates("name")
    def _validate_name(self, _key: str, value: str) -> str:
        return normalize_required_text(value, field="name", maximum=200)

    @validates("category_code")
    def _validate_category(self, _key: str, value: str) -> str:
        return normalize_category_code(value)

    @validates("description")
    def _validate_description(self, _key: str, value: str) -> str:
        return normalize_required_text(value, field="description", maximum=4000)


class SellableItem(Base):
    __tablename__ = "sellable_items"
    __table_args__ = (
        CheckConstraint(
            "model_label IS NULL OR char_length(btrim(model_label)) BETWEEN 1 AND 100",
            name="chk_sellable_items_model_label_length",
        ),
        CheckConstraint(
            "sku IS NULL OR (sku = upper(btrim(sku)) "
            "AND sku ~ '^[A-Z0-9][A-Z0-9._-]{0,63}$')",
            name="chk_sellable_items_sku_format",
        ),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name="chk_sellable_items_attributes_object",
        ),
        CheckConstraint(
            "octet_length(attributes::text) <= 4096",
            name="chk_sellable_items_attributes_size",
        ),
        CheckConstraint(
            "NOT jsonb_path_exists(attributes, "
            "'$.* ? (@.type() == \"object\" || @.type() == \"array\" || "
            "@.type() == \"null\" || @.type() == \"number\" && @ % 1 != 0)')",
            name="chk_sellable_items_attributes_primitives",
        ),
        Index(
            "uq_sellable_items_sku",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL"),
        ),
        Index("idx_sellable_items_product_active", "product_id", "active"),
        {"schema": "mbb"},
    )

    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.products.product_id",
            name="fk_sellable_items_product_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    model_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict[str, str | int | bool]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    @validates("model_label")
    def _validate_model_label(self, _key: str, value: str | None) -> str | None:
        return normalize_optional_label(value)

    @validates("sku")
    def _validate_sku(self, _key: str, value: str | None) -> str | None:
        return normalize_sku(value)

    @validates("attributes")
    def _validate_attributes(
        self, _key: str, value: Any
    ) -> dict[str, str | int | bool]:
        return validate_sellable_attributes(value)
