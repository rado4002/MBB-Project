"""
app/modules/m8_maps/tagger.py — MAPS tag capture logic.

Extracts demand signals from messages using keyword detection + AI classification.
Tags are persisted asynchronously via Celery (never blocks message response).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.maps_tag import MapsTag
from app.models.message import Message
from app.models.conversation import Conversation
from app.modules.m8_maps.categories import CATEGORY_PATTERNS, VALID_CATEGORIES

log = structlog.get_logger(__name__)

# ── Keyword patterns for rule-based tagging ───────────────────────────────────
_PRODUCT_KEYWORDS = re.compile(
    r"(cable|hdmi|chargeur|écouteur|téléphone|phone|batterie|coque|accessoire|"
    r"écran|adaptateur|usb|casque|montre|laptop|tablette)",
    re.IGNORECASE,
)

_PRICE_OBJECTION = re.compile(
    r"(cher|trop cher|prix|coûte|ntalo|mosolo|mbongo|budget|pas les moyens)",
    re.IGNORECASE,
)

_HUMAN_REQUEST = re.compile(
    r"(parler.*(quelqu|humain|personne|agent)|nabingi\s+moto|"
    r"veux.*parler|je veux.*un humain|nalingi.*moto)",
    re.IGNORECASE,
)

_OPT_OUT = re.compile(
    r"^(stop|arrête|yaka te|tika|désabonner|unsubscribe|laisse.?moi)$",
    re.IGNORECASE,
)

_URGENCY_CUES = re.compile(
    r"(urgent|vite|rapidement|noki|maintenant|aujourd.?hui|besoin.*tout de suite)",
    re.IGNORECASE,
)


async def tag_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    customer_id: str,
    content: str,
    content_type: str,
    language: str,
    city: str | None = None,
) -> list[dict[str, Any]]:
    """
    Analyze a message and create MAPS tags. Returns list of created tag dicts.

    Rule-based extraction (fast, no AI call):
      - Product mentions → product_demand
      - Price objections → silence_reason:price_high
      - Opt-out keywords → opt_out_reason
      - Language detection → language_usage
      - Urgency cues → conversion_trigger:urgency
    """
    tags_created: list[dict[str, Any]] = []

    # 1. Language usage (always tagged)
    lang_pattern = language if language in ("lingala", "french", "swahili") else "mixed"
    tags_created.append(
        await _create_tag(
            session, message_id, conversation_id, customer_id,
            "language_usage", lang_pattern, city=city, language=language,
        )
    )

    # 2. Product demand
    match = _PRODUCT_KEYWORDS.search(content)
    if match:
        tags_created.append(
            await _create_tag(
                session, message_id, conversation_id, customer_id,
                "product_demand", "product_name", city=city, language=language,
                metadata={"product_keyword": match.group(0).lower()},
            )
        )

    # 3. Price objection → silence_reason
    if _PRICE_OBJECTION.search(content):
        tags_created.append(
            await _create_tag(
                session, message_id, conversation_id, customer_id,
                "silence_reason", "price_high", city=city, language=language,
            )
        )

    # 4. Opt-out
    if _OPT_OUT.search(content.strip()):
        tags_created.append(
            await _create_tag(
                session, message_id, conversation_id, customer_id,
                "opt_out_reason", "not_interested", city=city, language=language,
            )
        )

    # 5. Urgency → conversion trigger
    if _URGENCY_CUES.search(content):
        tags_created.append(
            await _create_tag(
                session, message_id, conversation_id, customer_id,
                "conversion_trigger", "urgency", city=city, language=language,
            )
        )

    return tags_created


async def _create_tag(
    session: AsyncSession,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    customer_id: str,
    category: str,
    pattern: str,
    *,
    city: str | None = None,
    language: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Persist a single MAPS tag and return its dict representation."""
    tag = MapsTag(
        message_id=message_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        category=category,
        pattern=pattern,
        city=city,
        language=language,
        tag_metadata=metadata or {},
    )
    session.add(tag)
    await session.flush()
    return {
        "tag_id": str(tag.tag_id),
        "category": tag.category,
        "pattern": tag.pattern,
    }


async def get_insights(
    session: AsyncSession,
    period_start: datetime,
    period_end: datetime,
    category: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Aggregate MAPS tags for a given period. Returns top patterns by count.
    Uses direct queries (materialized view is for dashboard caching).
    """
    query = (
        select(
            MapsTag.category,
            MapsTag.pattern,
            func.count().label("cnt"),
        )
        .where(MapsTag.created_at >= period_start)
        .where(MapsTag.created_at <= period_end)
    )
    if category:
        query = query.where(MapsTag.category == category)

    query = query.group_by(MapsTag.category, MapsTag.pattern).order_by(func.count().desc()).limit(limit)
    result = await session.execute(query)
    rows = result.all()

    total = sum(r.cnt for r in rows)
    insights = [
        {"tag": r.pattern, "category": r.category, "count": r.cnt, "trend": None}
        for r in rows
    ]

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total_tags": total,
        "insights": insights,
    }


async def validate_tag(
    session: AsyncSession,
    tag_id: uuid.UUID,
    validated: bool,
    corrected_tag: str | None = None,
    corrected_category: str | None = None,
) -> dict[str, Any]:
    """Admin: validate or correct an AI-generated tag."""
    tag = await session.get(MapsTag, tag_id)
    if not tag:
        raise ValueError(f"Tag {tag_id} not found")

    if corrected_tag:
        tag.pattern = corrected_tag
    if corrected_category and corrected_category in VALID_CATEGORIES:
        tag.category = corrected_category

    # Store validation in metadata
    meta = dict(tag.tag_metadata) if tag.tag_metadata else {}
    meta["validated"] = validated
    meta["validated_at"] = datetime.now(timezone.utc).isoformat()
    tag.tag_metadata = meta

    await session.flush()
    return {
        "tag_id": str(tag.tag_id),
        "validated": validated,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def list_tags(
    session: AsyncSession,
    validated: bool | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Admin: list raw tags with optional filters. Returns (tags, total)."""
    base = select(MapsTag)
    count_q = select(func.count()).select_from(MapsTag)

    if category:
        base = base.where(MapsTag.category == category)
        count_q = count_q.where(MapsTag.category == category)

    if validated is not None:
        # Filter on metadata->>'validated'
        if validated:
            base = base.where(MapsTag.tag_metadata["validated"].as_boolean() == True)  # noqa: E712
            count_q = count_q.where(MapsTag.tag_metadata["validated"].as_boolean() == True)  # noqa: E712
        else:
            base = base.where(
                MapsTag.tag_metadata["validated"].is_(None)
                | (MapsTag.tag_metadata["validated"].as_boolean() == False)  # noqa: E712
            )
            count_q = count_q.where(
                MapsTag.tag_metadata["validated"].is_(None)
                | (MapsTag.tag_metadata["validated"].as_boolean() == False)  # noqa: E712
            )

    total = (await session.execute(count_q)).scalar() or 0
    result = await session.execute(
        base.order_by(MapsTag.created_at.desc()).offset(offset).limit(limit)
    )
    tags = result.scalars().all()

    return (
        [
            {
                "tag_id": str(t.tag_id),
                "message_id": str(t.message_id),
                "conversation_id": str(t.conversation_id),
                "category": t.category,
                "pattern": t.pattern,
                "language": t.language,
                "city": t.city,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "validated": t.tag_metadata.get("validated") if t.tag_metadata else None,
            }
            for t in tags
        ],
        total,
    )
