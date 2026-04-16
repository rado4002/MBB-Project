"""
app/modules/m5_qualification/service.py — Lead creation + qualification flow.

Implements:
  1. Lead scoring and creation (qualify_and_create_lead)
  2. Progressive qualification flow (2-3 smart questions)
  3. Information extraction (city, budget, urgency)

Called by the M1 Celery task when qualification signals are detected.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n.messages import t
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.modules.m5_qualification.scorer import score_lead

log = structlog.get_logger(__name__)

QualificationStep = Literal["q1_intent", "q2_location", "q3_urgency", "complete"]


async def qualify_and_create_lead(
    *,
    session: AsyncSession,
    customer_phone: str,
    conversation_id: uuid.UUID,
    message_text: str,
    msg_count: int = 1,
    response_speed_s: float | None = None,
) -> Lead | None:
    """
    Score the customer's message and create a Lead if qualification threshold met.

    Returns:
        Lead instance if created, None if score too low or lead already exists.
    """
    # Check if lead already exists for this conversation
    existing = await session.execute(
        select(Lead).where(Lead.conversation_id == conversation_id)
    )
    if existing.scalar_one_or_none() is not None:
        log.debug("m5.lead_exists", conv_id=str(conversation_id))
        return None

    # Score the lead
    score_value, score_label, intent = score_lead(
        message_text,
        msg_count=msg_count,
        response_speed_s=response_speed_s,
    )

    # Only create leads for warm+ scores (40+) — Phase 1.B spec
    if score_value < 40:
        log.debug(
            "m5.below_threshold",
            conv_id=str(conversation_id),
            score=score_value,
        )
        return None

    # Determine initial stage based on score (using stages.py)
    from app.modules.m5_qualification.stages import suggest_stage_from_score
    initial_stage = suggest_stage_from_score(score_value)

    now = datetime.now(timezone.utc)
    lead = Lead(
        lead_id=uuid.uuid4(),
        customer_id=customer_phone,
        conversation_id=conversation_id,
        score=score_label,
        score_value=score_value,
        stage=initial_stage,
        intent=intent,
        product_interest=[],
        source="whatsapp_auto",
        relance_count=0,
        qualified_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(lead)
    await session.flush()

    log.info(
        "m5.lead_created",
        lead_id=str(lead.lead_id),
        conv_id=str(conversation_id),
        phone=customer_phone,
        score=score_label,
        score_value=score_value,
        intent=intent,
    )

    # Dispatch CRM sync via Celery (best-effort)
    try:
        from app.tasks.celery_app import celery_app
        celery_app.send_task(
            "m5.sync_lead_to_crm",
            kwargs={
                "lead_id": str(lead.lead_id),
                "customer_phone": customer_phone,
                "score": score_label,
                "score_value": score_value,
                "intent": intent,
            },
            queue="default",
        )
    except Exception as exc:
        log.warning("m5.crm_sync_dispatch_failed", error=str(exc))

    return lead


# ── Qualification Flow (Progressive Questioning) ────────────────────────────


async def get_next_qualification_question(
    *,
    conversation: Conversation,
    language: str,
) -> str | None:
    """
    Determine the next qualification question to ask (Q1 → Q2 → Q3).

    Returns:
        Question text in customer's language, or None if qualification complete.
    """
    context = conversation.context or {}
    qual_state = context.get("qualification_state", {})
    current_step: QualificationStep = qual_state.get("step", "q1_intent")  # type: ignore[assignment]

    # If already complete, no more questions
    if current_step == "complete":
        return None

    # Check if we have minimum info to skip ahead
    has_city = bool(qual_state.get("city"))
    has_urgency = bool(qual_state.get("urgency"))

    # Q1: Intent (what are you looking for?)
    if current_step == "q1_intent":
        return t("qualification_q1", language)

    # Q2: Location (where are you?)
    elif current_step == "q2_location":
        # Skip if we already detected city
        if has_city:
            qual_state["step"] = "q3_urgency"
            return get_next_qualification_question(
                conversation=conversation, language=language
            )
        return t("qualification_q2", language)

    # Q3: Urgency (when do you need it?)
    elif current_step == "q3_urgency":
        # Skip if not critical or already have urgency
        if has_urgency or has_city:
            qual_state["step"] = "complete"
            return None
        return t("qualification_q3", language)

    return None


async def process_qualification_answer(
    *,
    conversation: Conversation,
    customer_message: str,
) -> dict:
    """
    Extract information from customer's answer and advance qualification flow.

    Returns:
        Extracted info dict: {city, product_interest, urgency, budget_range}
    """
    context = conversation.context or {}
    qual_state = context.get("qualification_state", {})
    current_step: QualificationStep = qual_state.get("step", "q1_intent")  # type: ignore[assignment]

    extracted = {
        "city": qual_state.get("city"),
        "product_interest": qual_state.get("product_interest", []),
        "urgency": qual_state.get("urgency"),
        "budget_range": qual_state.get("budget_range"),
    }

    lower = customer_message.lower()

    # ── Q1 Answer: Extract product interest and budget hints ────────────────
    if current_step == "q1_intent":
        # Extract product keywords
        products = _extract_products(lower)
        if products:
            extracted["product_interest"] = products

        # Extract budget hints
        budget = _extract_budget(customer_message)
        if budget:
            extracted["budget_range"] = budget

        # Advance to Q2
        qual_state["step"] = "q2_location"

    # ── Q2 Answer: Extract city ─────────────────────────────────────────────
    elif current_step == "q2_location":
        city = _extract_city(lower)
        if city:
            extracted["city"] = city

        # Advance to Q3
        qual_state["step"] = "q3_urgency"

    # ── Q3 Answer: Extract urgency ──────────────────────────────────────────
    elif current_step == "q3_urgency":
        urgency = _extract_urgency(lower)
        if urgency:
            extracted["urgency"] = urgency

        # Mark qualification complete
        qual_state["step"] = "complete"

    # Update qualification state in conversation context
    qual_state.update(extracted)
    context["qualification_state"] = qual_state
    conversation.context = context

    log.info(
        "m5.qual.answer_processed",
        conv_id=str(conversation.conversation_id),
        step=current_step,
        extracted=extracted,
    )

    return extracted


# ── Information Extraction Helpers ──────────────────────────────────────────


def _extract_products(text: str) -> list[str]:
    """Extract product keywords from text."""
    product_keywords = {
        "câble", "cable", "hdmi", "usb", "type-c",
        "téléphone", "telephone", "phone", "smartphone",
        "chargeur", "charger", "adaptateur", "adapter",
        "écouteur", "earphones", "headphones", "écouteurs",
        "powerbank", "batterie", "battery",
        "coque", "case", "protection",
        "écran", "screen",
    }
    words = set(re.findall(r"\b[a-zàâäéèêëîïôùûüçæœ'-]+\b", text))
    found = words & product_keywords
    return list(found) if found else []


def _extract_city(text: str) -> str | None:
    """Extract city name from text."""
    city_map = {
        "kinshasa": "Kinshasa",
        "kin": "Kinshasa",
        "lubumbashi": "Lubumbashi",
        "lushi": "Lubumbashi",
        "mbuji-mayi": "Mbuji-Mayi",
        "mbujimayi": "Mbuji-Mayi",
        "kisangani": "Kisangani",
        "kananga": "Kananga",
        "goma": "Goma",
        "bukavu": "Bukavu",
        "matadi": "Matadi",
    }
    text_lower = text.lower()
    for keyword, city in city_map.items():
        if keyword in text_lower:
            return city
    return None


def _extract_urgency(text: str) -> str | None:
    """Extract urgency level from text."""
    urgent_keywords = {"maintenant", "aujourd'hui", "lelo", "leo", "vite", "rapide", "haraka"}
    flexible_keywords = {"demain", "lobi", "kesho", "semaine", "wiki", "juma", "mois"}

    words = set(re.findall(r"\b[a-zàâäéèêëîïôùûüçæœ'-]+\b", text))

    if words & urgent_keywords:
        return "urgent"
    elif words & flexible_keywords:
        return "flexible"
    else:
        return "unknown"


def _extract_budget(text: str) -> str | None:
    """Extract budget range from text (in CDF)."""
    # Look for numbers followed by CDF or franc
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:cdf|franc|fc)", text, re.IGNORECASE)
    if matches:
        # Convert to int and categorize
        amount = int(float(matches[0].replace(",", "")))
        if amount < 5000:
            return "0-5000"
        elif amount < 20000:
            return "5000-20000"
        elif amount < 50000:
            return "20000-50000"
        else:
            return "50000+"
    return None

