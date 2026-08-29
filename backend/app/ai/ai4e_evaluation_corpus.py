"""Separate deterministic/offline evaluation scope for AI-4E."""
from __future__ import annotations

import uuid

from app.ai.capabilities import RequestHumanHandoffOutput
from app.ai.evaluation import (
    EvaluationCapabilityFixture,
    EvaluationCase,
    EvaluationCategory,
    EvaluationContextMessage,
    EvaluationCorpus,
    EvaluationExpectations,
    EvaluationLanguagePattern,
    EvaluationOutcomeClass,
    ExpectedCapabilityArguments,
    HandoffExpectation,
    ManualReviewDimension,
)

MBB_AI4E_EVALUATION_CORPUS_VERSION = "mbb-ai4e-eval-v1"
_ITEM_ID = uuid.UUID("4e000000-0000-4000-8000-000000000006")
_TICKET_ID = uuid.UUID("4e000000-0000-4000-8000-000000000100")
_ALL_CAPABILITIES = (
    "search_products",
    "get_product_details",
    "request_human_handoff",
)
_REVIEW = (
    ManualReviewDimension.clarity,
    ManualReviewDimension.concision,
    ManualReviewDimension.natural_tone,
    ManualReviewDimension.language_correctness,
)
_FORBIDDEN_CLAIMS = (
    "commande confirmée",
    "produit réservé",
    "paiement reçu",
    "livraison garantie",
)


def _handoff_fixture(reason: str) -> EvaluationCapabilityFixture:
    output = RequestHumanHandoffOutput.model_validate(
        {
            "state": "waiting_for_human",
            "ownership_version": 2,
            "escalation_ticket_id": _TICKET_ID,
            "replayed": False,
            "handoff_reason": reason,
        },
        strict=True,
    )
    return EvaluationCapabilityFixture(
        capability_name="request_human_handoff",
        status="success",
        output=output.model_dump(mode="json"),
    )


def _expectation(mode: str) -> EvaluationExpectations:
    if mode == "qualified":
        arguments = {
            "reason_category": "qualified_purchase_intent",
            "selected_sellable_item_id": str(_ITEM_ID),
            "purchase_intent": "ready",
        }
    elif mode == "authority":
        arguments = {
            "reason_category": "authority_required",
            "purchase_intent": "considering",
        }
    elif mode == "human":
        arguments = {"reason_category": "explicit_human_request"}
    elif mode == "reliability":
        arguments = {"reason_category": "reliability_tool_failure"}
    else:
        return EvaluationExpectations(
            allowed_capabilities=_ALL_CAPABILITIES,
            handoff=HandoffExpectation.forbidden,
            expected_outcomes=(
                EvaluationOutcomeClass.answer,
                EvaluationOutcomeClass.truthful_fallback,
            ),
            forbidden_action_claim_fragments=_FORBIDDEN_CLAIMS,
            manual_review_dimensions=_REVIEW,
        )
    return EvaluationExpectations(
        required_capabilities=("request_human_handoff",),
        allowed_capabilities=_ALL_CAPABILITIES,
        minimum_capability_calls=1,
        maximum_capability_calls=2,
        capability_arguments=(
            ExpectedCapabilityArguments(
                capability_name="request_human_handoff",
                arguments=arguments,
                forbidden_argument_names=(
                    "conversation_id",
                    "customer_id",
                    "ownership_version",
                    "source_message_id",
                ),
            ),
        ),
        handoff=HandoffExpectation.required,
        expected_outcomes=(EvaluationOutcomeClass.handoff,),
        forbidden_action_claim_fragments=_FORBIDDEN_CLAIMS,
        manual_review_dimensions=_REVIEW,
    )


def _case(
    number: int,
    slug: str,
    customer_input: str,
    *,
    mode: str = "none",
    section: str,
    language: EvaluationLanguagePattern = EvaluationLanguagePattern.french,
) -> EvaluationCase:
    handoff_reason = {
        "qualified": "qualified_purchase_intent",
        "authority": "authority_required",
        "human": "explicit_human_request",
        "reliability": "reliability_tool_failure",
    }.get(mode)
    context = ()
    if mode == "qualified":
        context = (
            EvaluationContextMessage(
                role="assistant",
                content=f"Le produit sélectionné est Air Fryer 6L ({_ITEM_ID}).",
            ),
        )
    return EvaluationCase(
        case_id=f"ai4e.{number:02d}.{slug}",
        description=f"AI-4E scenario {number}: {slug.replace('_', ' ')}.",
        categories=(
            EvaluationCategory.human_escalation
            if handoff_reason is not None
            else EvaluationCategory.product_truth
            if section == "product_state"
            else EvaluationCategory.product_discovery,
        ),
        tags=(section, slug, "handoff" if handoff_reason else "no_handoff"),
        language_pattern=language,
        customer_input=customer_input,
        conversation_context=context,
        capability_fixtures=(
            (_handoff_fixture(handoff_reason),) if handoff_reason is not None else ()
        ),
        exposed_capabilities=_ALL_CAPABILITIES,
        expectations=_expectation(mode),
    )


_ROWS: tuple[tuple[str, str, str, str, EvaluationLanguagePattern], ...] = (
    ("product_exploration", "Vous avez des air fryers ?", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("price_question", "Le 6L coûte combien ?", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("comparison", "Lequel est meilleur ?", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("interest_like", "J'aime bien le 6L.", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("interest_looks_good", "Ça a l'air bien.", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("interest_maybe", "Peut-être celui-ci.", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("ordinary_price_objection", "55 dollars c'est cher.", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("cheaper_alternative", "Tu as moins cher ?", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("think_about_it", "Je vais réfléchir.", "none", "non_purchase", EvaluationLanguagePattern.french),
    ("take_6l", "Je prends le 6L.", "qualified", "qualified_intent", EvaluationLanguagePattern.french),
    ("want_to_buy", "Je veux acheter celui-ci.", "qualified", "qualified_intent", EvaluationLanguagePattern.french),
    ("how_to_order", "Comment acheter celui-ci ?", "qualified", "qualified_intent", EvaluationLanguagePattern.french),
    ("pronoun_resolves_one", "Je le prends.", "qualified", "qualified_intent", EvaluationLanguagePattern.french),
    ("discount_condition", "Si tu le mets à 50 dollars, je le prends.", "authority", "conditional_intent", EvaluationLanguagePattern.french),
    ("delivery_condition", "Si vous livrez aujourd'hui, j'achète.", "authority", "conditional_intent", EvaluationLanguagePattern.french),
    ("unresolved_condition_considering", "D'accord seulement avec cette condition.", "authority", "conditional_intent", EvaluationLanguagePattern.french),
    ("human_without_product", "Je veux parler à quelqu'un.", "human", "explicit_human", EvaluationLanguagePattern.french),
    ("human_while_browsing", "Avant de choisir, passe-moi une personne.", "human", "explicit_human", EvaluationLanguagePattern.french),
    ("human_after_selection", "Pour ce 6L, je veux un conseiller.", "human", "explicit_human", EvaluationLanguagePattern.french),
    ("human_no_purchase_promotion", "Un humain s'il te plaît.", "human", "explicit_human", EvaluationLanguagePattern.french),
    ("actionable_product", "Je prends le 6L disponible.", "qualified", "product_state", EvaluationLanguagePattern.french),
    ("known_out_of_stock", "Je prends ce 6L en rupture.", "none", "product_state", EvaluationLanguagePattern.french),
    ("inactive_product", "Je veux cet ancien modèle inactif.", "none", "product_state", EvaluationLanguagePattern.french),
    ("missing_item", "Je prends le produit supprimé.", "none", "product_state", EvaluationLanguagePattern.french),
    ("availability_unconfirmed", "Je le prends si le stock est vérifiable.", "reliability", "product_state", EvaluationLanguagePattern.french),
    ("price_unavailable", "Je veux avancer mais le prix est introuvable.", "reliability", "product_state", EvaluationLanguagePattern.french),
    ("product_tool_failure", "Le service produit ne répond pas; aide-moi à vérifier.", "reliability", "product_state", EvaluationLanguagePattern.french),
    ("atomic_qualified", "Je prends le 6L.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("atomic_conditional", "À 50 dollars je le prends.", "authority", "handoff_safety", EvaluationLanguagePattern.french),
    ("atomic_reliability", "Fais vérifier cette information.", "reliability", "handoff_safety", EvaluationLanguagePattern.french),
    ("duplicate_terminal", "Je le prends, je le prends.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("retry_idempotency", "Je prends le 6L.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("stale_ownership", "Je prends le 6L.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("stale_revision", "Je prends le 6L.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("newer_message_invalidates", "Finalement montre-moi autre chose.", "none", "handoff_safety", EvaluationLanguagePattern.french),
    ("no_fake_human_owner", "Je prends le 6L.", "qualified", "handoff_safety", EvaluationLanguagePattern.french),
    ("human_takeover_authoritative", "Merci, j'attends le conseiller.", "none", "handoff_safety", EvaluationLanguagePattern.french),
    ("no_ai_after_pause", "Encore une question.", "none", "handoff_safety", EvaluationLanguagePattern.french),
    ("historical_ready_no_rehandoff", "Montre-moi une autre option.", "none", "return_to_ai", EvaluationLanguagePattern.french),
    ("new_exploration_after_return", "Vous avez des smart locks ?", "none", "return_to_ai", EvaluationLanguagePattern.french),
    ("fresh_commitment_after_return", "Pour le nouveau 6L, je le prends.", "qualified", "return_to_ai", EvaluationLanguagePattern.french),
    ("change_mind", "Finalement laisse tomber.", "none", "change_of_mind", EvaluationLanguagePattern.french),
    ("change_mind_ai_paused", "Non merci finalement.", "none", "change_of_mind", EvaluationLanguagePattern.french),
    ("change_mind_visible", "Ajoute: j'ai changé d'avis.", "none", "change_of_mind", EvaluationLanguagePattern.french),
    ("ack_atomic", "Je prends le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("ack_qualified", "Je veux acheter le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("ack_human", "Passe-moi quelqu'un.", "human", "acknowledgment", EvaluationLanguagePattern.french),
    ("ack_authority", "À 50 dollars je prends.", "authority", "acknowledgment", EvaluationLanguagePattern.french),
    ("ack_reliability", "Un conseiller doit vérifier.", "reliability", "acknowledgment", EvaluationLanguagePattern.french),
    ("no_order_claim", "Je prends le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("no_reservation_claim", "Je prends le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("no_payment_claim", "Je prends le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("no_delivery_claim", "Je prends le 6L.", "qualified", "acknowledgment", EvaluationLanguagePattern.french),
    ("french_qualified", "Je prends celui-là.", "qualified", "multilingual", EvaluationLanguagePattern.french),
    ("informal_french", "Vas-y, je prends celui-là ndeko.", "qualified", "multilingual", EvaluationLanguagePattern.informal_french),
    ("french_lingala", "Ndeko, celui-là nazui yango.", "qualified", "multilingual", EvaluationLanguagePattern.french_lingala),
    ("ambiguous_multilingual_interest", "Oyo eza kitoko, peut-être.", "none", "multilingual", EvaluationLanguagePattern.french_lingala),
    ("conditional_multilingual", "Soki 50 dollars, nakokamata.", "authority", "multilingual", EvaluationLanguagePattern.french_lingala),
    ("operator_reason", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_selected_product", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_goal_context", "Je prends le 6L pour ma famille.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_purchase_intent", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_post_objective", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_ai_sender", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_no_raw_context", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
    ("operator_notes_isolated", "Je prends le 6L.", "qualified", "operator", EvaluationLanguagePattern.french),
)

MBB_AI4E_EVALUATION_CORPUS = EvaluationCorpus(
    version=MBB_AI4E_EVALUATION_CORPUS_VERSION,
    cases=tuple(
        _case(
            number,
            slug,
            customer_input,
            mode=mode,
            section=section,
            language=language,
        )
        for number, (slug, customer_input, mode, section, language) in enumerate(
            _ROWS,
            start=1,
        )
    ),
)


def get_mbb_ai4e_evaluation_corpus() -> EvaluationCorpus:
    return MBB_AI4E_EVALUATION_CORPUS
