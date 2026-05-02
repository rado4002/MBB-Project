"""
app/modules/m6_relance/hooks.py — Value hook generator for relances.

Generates persuasive, culturally-appropriate relance messages using Claude API.
Each relance uses a different persuasion angle (3 attempts maximum).

Hook Types (per Phase 1.B spec):
  1. Relance #1 (+24h): Value reminder — "Hey! The product you looked at is still available"
  2. Relance #2 (+48-72h): Social proof — "Others in [city] are loving this..."
  3. Relance #3 (+7-10d): Exclusive offer — "Last chance — special offer just for you"
"""
from __future__ import annotations

import structlog

from app.adapters import get_ai_adapter
from app.i18n import messages as i18n
from app.schemas.common import Language

log = structlog.get_logger(__name__)


async def generate_relance_hook(
    *,
    attempt_number: int,
    language: Language,
    product_interest: str | None,
    city: str | None,
    customer_name: str | None,
    previous_hooks: list[str] | None = None,
) -> tuple[str, str]:
    """
    Generate a value-first relance message using Claude API.

    Args:
        attempt_number: 1, 2, or 3
        language: Customer's preferred language
        product_interest: Product the customer asked about
        city: Customer's city (for social proof)
        customer_name: Customer's name (if known)
        previous_hooks: List of previous hook texts (to avoid repetition)

    Returns:
        Tuple of (hook_text, hook_type)
        - hook_text: The relance message to send (2-3 sentences max)
        - hook_type: One of 'reciprocity', 'social_proof', 'scarcity', 'loyalty'

    Raises:
        ValueError: If attempt_number not in 1, 2, 3
    """
    if attempt_number not in [1, 2, 3]:
        raise ValueError(f"Invalid attempt_number: {attempt_number} (must be 1, 2, or 3)")

    # Determine hook angle based on attempt
    if attempt_number == 1:
        hook_type = "reciprocity"  # Value reminder
        angle_prompt = "Remind them of the product value. Friendly check-in. No pressure."
    elif attempt_number == 2:
        hook_type = "social_proof"  # Others are buying
        angle_prompt = f"Use social proof: others in {city or 'Kinshasa'} are loving this product. Build FOMO gently."
    else:  # attempt_number == 3
        hook_type = "scarcity"  # Final offer
        angle_prompt = "Final attempt: exclusive offer or limited availability. Last chance but still friendly."

    # Build prompt for Claude
    prompt = _build_hook_prompt(
        angle=angle_prompt,
        language=language,
        product_interest=product_interest,
        city=city,
        customer_name=customer_name,
        previous_hooks=previous_hooks,
    )

    # Call Claude API
    ai_adapter = get_ai_adapter()
    system_instruction = "You are a friendly Congolese sales assistant. Generate warm, persuasive WhatsApp messages. 2-3 sentences MAX. No emojis unless natural for the language."
    try:
        hook_text = await ai_adapter.generate(
            prompt=prompt,
            system=system_instruction,
            max_tokens=300,
        )
    except Exception as e:
        log.error("m6.hooks.claude_error", error=str(e), attempt=attempt_number)
        # Fallback to i18n template
        hook_text = i18n.t(f"relance_fallback_{attempt_number}", language)

    log.info(
        "m6.hooks.generated",
        attempt=attempt_number,
        hook_type=hook_type,
        language=language,
        hook_length=len(hook_text),
    )

    return hook_text, hook_type


def _build_hook_prompt(
    *,
    angle: str,
    language: Language,
    product_interest: str | None,
    city: str | None,
    customer_name: str | None,
    previous_hooks: list[str] | None,
) -> str:
    """Build Claude prompt for hook generation."""
    lang_name = {
        Language.french: "French",
        Language.lingala: "Lingala",
        Language.swahili: "Swahili",
    }[language]

    prompt_parts = [
        f"Generate a WhatsApp relance message in {lang_name}.",
        f"Angle: {angle}",
        f"Product: {product_interest or 'electronics/tech accessories'}",
    ]

    if city:
        prompt_parts.append(f"Customer location: {city}")

    if customer_name:
        prompt_parts.append(f"Customer name: {customer_name} (use if natural)")

    if previous_hooks:
        prompt_parts.append(f"Previous hooks sent (DO NOT REPEAT): {', '.join(previous_hooks[:50])}")

    prompt_parts.extend([
        "",
        "Requirements:",
        "- 2-3 sentences maximum",
        "- Warm, friendly tone (like a helpful young Congolese friend)",
        "- Value-first, not pushy",
        "- Natural conversational style for WhatsApp",
        "- Include 1 emoji if appropriate for the language",
        "",
        "Return ONLY the message text, nothing else."
    ])

    return "\n".join(prompt_parts)


def get_fallback_hook(attempt_number: int, language: Language) -> str:
    """
    Get fallback hook from i18n if Claude fails.

    Args:
        attempt_number: 1, 2, or 3
        language: Customer's language

    Returns:
        Fallback hook text
    """
    key = f"relance_fallback_{attempt_number}"
    return i18n.t(key, language)
