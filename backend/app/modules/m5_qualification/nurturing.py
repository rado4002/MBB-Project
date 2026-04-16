"""
app/modules/m5_qualification/nurturing.py — Product recommendations + persuasion hooks.

Generates nurturing responses based on lead stage and conversation context:
  - HOT leads → immediate product recommendations with CTA
  - WARM leads → value content + social proof + gentle product suggestions
  - COLD leads → light touch, educational content

Uses Claude AI to generate contextual, persuasive responses in the customer's language.
"""
from __future__ import annotations

import structlog
from anthropic import Anthropic

from app.adapters import get_ai_adapter
from app.i18n.messages import t

log = structlog.get_logger(__name__)


async def generate_nurturing_response(
    *,
    lead_stage: str,
    score_label: str,
    intent: str,
    product_interest: list[str],
    language: str,
    conversation_history: list[dict],
    customer_message: str,
) -> str:
    """
    Generate a nurturing response tailored to lead stage and intent.

    Args:
        lead_stage: awareness | consideration | decision
        score_label: hot | warm | cold
        intent: Detected intent (e.g., "product_specific,price_inquiry")
        product_interest: List of product categories mentioned
        language: Customer's detected language (lingala | french | swahili)
        conversation_history: Recent message history for context
        customer_message: Latest customer message

    Returns:
        Nurturing response string (max 2-3 sentences, warm tone)
    """
    ai = get_ai_adapter()

    # Build prompt based on lead stage
    system_prompt = _build_nurturing_prompt(
        lead_stage=lead_stage,
        score_label=score_label,
        intent=intent,
        product_interest=product_interest,
        language=language,
    )

    # Build conversation context
    context_messages = _format_conversation_for_claude(conversation_history)
    context_messages.append({
        "role": "user",
        "content": customer_message,
    })

    try:
        response = await ai.generate_response(
            system_prompt=system_prompt,
            messages=context_messages,
            max_tokens=300,  # Keep responses concise (2-3 sentences)
        )
        log.info(
            "m5.nurturing.generated",
            lead_stage=lead_stage,
            score_label=score_label,
            intent=intent,
            response_length=len(response),
        )
        return response

    except Exception as e:
        log.error(
            "m5.nurturing.failed",
            error=str(e),
            lead_stage=lead_stage,
        )
        # Fallback to generic response
        return t("nurturing_fallback", language)


def _build_nurturing_prompt(
    *,
    lead_stage: str,
    score_label: str,
    intent: str,
    product_interest: list[str],
    language: str,
) -> str:
    """Build a Claude system prompt tailored to lead stage and intent."""

    # Base persona (DRC-specific, warm tone)
    lang_name = {"lingala": "Lingala", "french": "French", "swahili": "Swahili"}.get(
        language, "French"
    )
    base = f"""You are a helpful product advisor for MBB ya Kin, a tech accessories shop in Kinshasa, DRC.

TONE: Warm, friendly, like a helpful young Congolese friend. NOT robotic, NOT pushy, NOT formal.
LANGUAGE: Respond ONLY in {lang_name}.
LENGTH: Max 2-3 sentences. Keep it concise.
GOAL: Help the customer find what they need while building trust.

"""

    # Stage-specific instructions
    if lead_stage == "decision" or score_label == "hot":
        stage_instructions = """STAGE: DECISION (hot lead — ready to buy)
ACTION: Recommend a specific product match with price and CTA.
HOOK: "This one's perfect for what you're looking for..."
EXAMPLE (French): "Le câble HDMI 2m est exactement ce qu'il te faut — 5000 CDF, livraison rapide à Kinshasa. Je te l'envoie ?"
"""

    elif lead_stage == "consideration" or score_label == "warm":
        stage_instructions = """STAGE: CONSIDERATION (warm lead — exploring)
ACTION: Nurture with value content + gentle product suggestion.
HOOK: "Others in Kinshasa love this..." or "This is popular because..."
EXAMPLE (Lingala): "Ndeko, charger oyo e-zali moko ya ba-best. Batu mingi ba-zali ko-somba. Olingi na-yebisa yo prix ?"
"""

    else:  # cold / awareness
        stage_instructions = """STAGE: AWARENESS (cold lead — browsing)
ACTION: Light touch, educational content, no hard sell.
HOOK: "What kind of [product] are you looking for?"
EXAMPLE (French): "On a plusieurs types de câbles. Tu cherches quoi exactement ?"
"""

    # Intent-specific hints
    intent_hints = ""
    if "price_inquiry" in intent:
        intent_hints += "Customer asked about price → provide price range or specific price.\n"
    if "city_mentioned" in intent:
        intent_hints += "Customer mentioned city → emphasize fast delivery to that location.\n"
    if "product_specific" in intent and product_interest:
        intent_hints += f"Customer interested in: {', '.join(product_interest)}\n"

    prompt = base + stage_instructions
    if intent_hints:
        prompt += f"\nCONTEXT:\n{intent_hints}"

    # DRC tone suffix (critical for cultural fit)
    prompt += """\n
CRITICAL RULES:
1. Feel like a helpful young Congolese friend, NOT a corporate bot
2. Use natural, casual language (no formal/stiff phrasing)
3. Max 2-3 sentences — Kinshasa customers prefer brevity
4. Help first, sell second
5. Never pushy, never aggressive
"""

    return prompt


def _format_conversation_for_claude(history: list[dict]) -> list[dict]:
    """Format conversation history for Claude API."""
    formatted = []
    for item in history[-10:]:  # Last 10 messages for context
        role = "assistant" if item.get("direction") == "outbound" else "user"
        formatted.append({
            "role": role,
            "content": item.get("content", ""),
        })
    return formatted


async def generate_product_suggestion(
    *,
    product_interest: list[str],
    budget_range: str | None,
    language: str,
) -> str:
    """
    Generate a specific product suggestion based on interest and budget.

    Returns a 1-2 sentence product recommendation.
    """
    ai = get_ai_adapter()

    prompt = f"""Suggest ONE specific tech accessory product (with price in CDF) that matches:
- Interest: {', '.join(product_interest) if product_interest else 'general tech accessories'}
- Budget: {budget_range or 'any'}
- Market: Kinshasa, DRC

Respond in {language.title()} in 1-2 sentences. Include product name and price.
Be specific (e.g., "Câble USB-C 1.5m — 3000 CDF").
"""

    try:
        response = await ai.generate_response(
            system_prompt=prompt,
            messages=[],
            max_tokens=150,
        )
        log.info(
            "m5.product_suggestion.generated",
            product_interest=product_interest,
            budget=budget_range,
        )
        return response

    except Exception as e:
        log.error("m5.product_suggestion.failed", error=str(e))
        # Fallback
        return t("product_suggestion_fallback", language)
