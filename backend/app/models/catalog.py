"""Authoritative Catalog persistence for product families and sellable items."""

from __future__ import annotations

import re
import uuid
from ipaddress import ip_address
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

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
MAX_PRODUCT_MEDIA_PER_OWNER = 10
MAX_MEDIA_ASSET_URL_LENGTH = 2048
MAX_MEDIA_ALT_TEXT_LENGTH = 500
MAX_MEDIA_DISPLAY_ORDER = 999
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


def normalize_media_asset_url(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_MEDIA_ASSET_URL_LENGTH:
        raise ValueError(
            f"asset_url must contain between 1 and {MAX_MEDIA_ASSET_URL_LENGTH} characters"
        )
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("asset_url must be a valid absolute HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("asset_url must be a valid absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("asset_url must not contain credentials")
    if any(character.isspace() for character in normalized):
        raise ValueError("asset_url must not contain whitespace")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("asset_url has an invalid port")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("asset_url must not reference localhost")
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("asset_url must not reference a non-public IP address")
    return normalized


def normalize_media_alt_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_MEDIA_ALT_TEXT_LENGTH:
        raise ValueError(
            f"alt_text must not exceed {MAX_MEDIA_ALT_TEXT_LENGTH} characters"
        )
    return normalized


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


class ProductMedia(Base):
    __tablename__ = "product_media"
    __table_args__ = (
        CheckConstraint(
            "(product_id IS NOT NULL AND sellable_item_id IS NULL) OR "
            "(product_id IS NULL AND sellable_item_id IS NOT NULL)",
            name="chk_product_media_exactly_one_owner",
        ),
        CheckConstraint(
            "char_length(asset_url) BETWEEN 1 AND 2048 "
            "AND lower(asset_url) LIKE 'https://%'",
            name="chk_product_media_asset_url",
        ),
        CheckConstraint(
            "alt_text IS NULL OR char_length(btrim(alt_text)) BETWEEN 1 AND 500",
            name="chk_product_media_alt_text_length",
        ),
        CheckConstraint(
            "display_order BETWEEN 0 AND 999",
            name="chk_product_media_display_order",
        ),
        CheckConstraint(
            "NOT is_primary OR active",
            name="chk_product_media_primary_active",
        ),
        Index(
            "uq_product_media_active_primary_product",
            "product_id",
            unique=True,
            postgresql_where=text(
                "product_id IS NOT NULL AND active AND is_primary"
            ),
        ),
        Index(
            "uq_product_media_active_primary_sellable_item",
            "sellable_item_id",
            unique=True,
            postgresql_where=text(
                "sellable_item_id IS NOT NULL AND active AND is_primary"
            ),
        ),
        Index(
            "idx_product_media_product_active_order",
            "product_id",
            "active",
            "display_order",
        ),
        Index(
            "idx_product_media_sellable_item_active_order",
            "sellable_item_id",
            "active",
            "display_order",
        ),
        {"schema": "mbb"},
    )

    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.products.product_id",
            name="fk_product_media_product_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    sellable_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.sellable_items.sellable_item_id",
            name="fk_product_media_sellable_item_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    asset_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    display_order: Mapped[int] = mapped_column(
        nullable=False, server_default=text("0")
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

    @validates("asset_url")
    def _validate_asset_url(self, _key: str, value: str) -> str:
        return normalize_media_asset_url(value)

    @validates("alt_text")
    def _validate_alt_text(self, _key: str, value: str | None) -> str | None:
        return normalize_media_alt_text(value)

    @validates("display_order")
    def _validate_display_order(self, _key: str, value: int) -> int:
        if type(value) is not int or not 0 <= value <= MAX_MEDIA_DISPLAY_ORDER:
            raise ValueError(
                f"display_order must be between 0 and {MAX_MEDIA_DISPLAY_ORDER}"
            )
        return value
