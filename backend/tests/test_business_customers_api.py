"""PostgreSQL/browser evidence for Customer Profile & Contact Preferences V1."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from app.api.v1 import business_customers
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_audit import OperatorAuditEvent
from app.models.relance import Relance
from app.models.relance_candidate import RelanceCandidate
from test_commerce_admin_api import (
    ADMIN_PASSWORD,
    OPERATOR_PASSWORD,
    ORIGIN,
    _headers,
    _login,
    harness,  # noqa: F401 -- shared disposable PostgreSQL/session fixture
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"),
    reason="AI2B_TEST_DATABASE_URL is required for Customer browser API evidence",
)


@pytest_asyncio.fixture(loop_scope="function")
async def customer_workspace(harness):  # noqa: F811
    transport, factory, administrator_id = harness
    transport.app.include_router(business_customers.router, prefix="/api/v1")
    customer_phone = "+243810005101"
    other_phone = "+243810005102"
    conversation_id = uuid.uuid4()
    other_conversation_id = uuid.uuid4()
    second_customer_conversation_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with factory() as db:
        await db.execute(text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE"))
        await db.commit()
    async with factory() as db:
        db.add_all(
            [
                Customer(
                    phone_number=customer_phone,
                    name="Marie Client",
                    city="Kinshasa",
                    preferred_language="french",
                    consent_given=False,
                ),
                Customer(
                    phone_number=other_phone,
                    name="Other Customer",
                    city="Goma",
                    preferred_language="swahili",
                ),
                Customer(
                    phone_number="+243810005103",
                    name="Customer Without Conversation",
                    city="Lubumbashi",
                    preferred_language="french",
                    last_interaction=now + timedelta(minutes=2),
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Conversation(
                    conversation_id=conversation_id,
                    customer_id=customer_phone,
                    language_detected="french",
                    last_message_time=now,
                ),
                Conversation(
                    conversation_id=other_conversation_id,
                    customer_id=other_phone,
                    language_detected="swahili",
                    last_message_time=now + timedelta(minutes=1),
                ),
                Conversation(
                    conversation_id=second_customer_conversation_id,
                    customer_id=customer_phone,
                    language_detected="french",
                    last_message_time=now - timedelta(minutes=1),
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Message(
                    message_id=evidence_id,
                    conversation_id=conversation_id,
                    timestamp=now,
                    direction="inbound",
                    inbound_receipt_order=1,
                    content="Arrête, ne plus contacter s'il vous plaît",
                    content_type="text",
                    language="french",
                    whatsapp_message_id=f"wa-{evidence_id}",
                ),
                Message(
                    conversation_id=other_conversation_id,
                    timestamp=now + timedelta(minutes=1),
                    direction="inbound",
                    inbound_receipt_order=1,
                    content="stop",
                    content_type="text",
                    language="swahili",
                    whatsapp_message_id=f"wa-{uuid.uuid4()}",
                ),
            ]
        )
        db.add(
            Lead(
                lead_id=lead_id,
                customer_id=customer_phone,
                conversation_id=conversation_id,
                score="warm",
                score_value=5,
                stage="consideration",
                intent="product_inquiry",
                product_interest=[],
                source="referral_partner",
                created_at=now,
            )
        )
        db.add(
            Lead(
                customer_id=customer_phone,
                conversation_id=second_customer_conversation_id,
                score="cold",
                score_value=1,
                stage="awareness",
                intent="product_inquiry",
                product_interest=[],
                source="later_campaign",
                created_at=now + timedelta(minutes=1),
            )
        )
        await db.flush()
        db.add_all(
            [
                Relance(
                    lead_id=lead_id,
                    attempt_number=1,
                    scheduled_at=now + timedelta(hours=1),
                    value_hook="Offline legacy follow-up",
                    hook_type="reciprocity",
                ),
                RelanceCandidate(
                    lead_id=lead_id,
                    source_message_id=evidence_id,
                    attempt_number=1,
                    scheduled_at=now + timedelta(hours=1),
                ),
            ]
        )
        await db.commit()
    try:
        yield (
            transport,
            factory,
            administrator_id,
            conversation_id,
            other_conversation_id,
            evidence_id,
        )
    finally:
        async with factory() as db:
            await db.execute(
                text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE")
            )
            await db.commit()


async def test_crm_read_is_authorized_full_identity_complete_and_customer_isolated(
    customer_workspace,
):
    transport, _factory, _administrator_id, conversation_id, other_id, evidence_id = (
        customer_workspace
    )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        assert (
            await client.get(f"/api/v1/business/customers/{conversation_id}")
        ).status_code == 401
        assert (await client.get("/api/v1/business/customers")).status_code == 401
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        overview = await client.get("/api/v1/business/customers?q=Marie")
        assert overview.status_code == 200
        assert overview.headers["cache-control"] == "no-store"
        overview_body = overview.json()
        assert overview_body["has_more"] is False
        assert len(overview_body["items"]) == 1
        assert overview_body["items"][0] | {
            "last_interaction_at": "checked",
            "customer_reference": "checked",
        } == {
            "customer_id": "+243810005101",
            "customer_reference": "checked",
            "conversation_id": str(conversation_id),
            "display_name": "Marie Client",
            "phone_number": "+243810005101",
            "communication_channel": "whatsapp",
            "acquisition_source": "referral_partner",
            "acquisition_source_provenance": "earliest_recorded_lead",
            "last_interaction_at": "checked",
        }
        all_customers = (await client.get("/api/v1/business/customers")).json()["items"]
        orphan = next(
            item for item in all_customers
            if item["customer_id"] == "+243810005103"
        )
        assert orphan["conversation_id"] is None
        assert orphan["acquisition_source"] is None
        assert orphan["communication_channel"] is None
        orphan_detail = await client.get(
            f"/api/v1/business/customers/by-reference/{orphan['customer_reference']}"
        )
        assert orphan_detail.status_code == 200
        assert orphan_detail.json()["customer_id"] == "+243810005103"
        assert orphan_detail.json()["conversation_id"] is None
        customer_reference = overview_body["items"][0]["customer_reference"]
        bound_detail = await client.get(
            f"/api/v1/business/customers/by-reference/{customer_reference}",
            params={"conversation_id": str(conversation_id)},
        )
        assert bound_detail.status_code == 200
        assert bound_detail.json()["conversation_id"] == str(conversation_id)
        mismatched_detail = await client.get(
            f"/api/v1/business/customers/by-reference/{customer_reference}",
            params={"conversation_id": str(other_id)},
        )
        assert mismatched_detail.status_code == 404
        response = await client.get(f"/api/v1/business/customers/{conversation_id}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["customer_id"] == "+243810005101"
        assert body["phone_number"] == "+243810005101"
        assert body["communication_channels"] == [
            {"channel": "whatsapp", "provenance": "verified_provider_message"}
        ]
        assert body["acquisition_sources"] == ["referral_partner", "later_campaign"]
        assert [item["message_id"] for item in body["opt_out_evidence"]] == [
            str(evidence_id)
        ]
        other = await client.get(f"/api/v1/business/customers/{other_id}")
        assert other.status_code == 200
        assert str(evidence_id) not in other.text


async def test_customer_overview_pagination_is_stable_and_customer_owned(
    customer_workspace,
):
    transport, factory, _administrator_id, _conversation_id, _other_id, _evidence_id = (
        customer_workspace
    )
    stable_time = datetime(2026, 10, 30, tzinfo=timezone.utc)
    async with factory() as db:
        db.add_all(
            [
                Customer(
                    phone_number=f"+24382000{index:04d}",
                    name=f"Pagination Customer {index:02d}",
                    city="Kinshasa",
                    preferred_language="french",
                    last_interaction=stable_time,
                )
                for index in range(27)
            ]
        )
        await db.execute(update(Customer).values(last_interaction=stable_time))
        await db.commit()

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        first = (await client.get("/api/v1/business/customers?offset=0")).json()
        second = (await client.get("/api/v1/business/customers?offset=25")).json()

    assert len(first["items"]) == 25 and first["has_more"] is True
    assert len(second["items"]) == 5 and second["has_more"] is False
    customer_ids = [
        item["customer_id"] for item in [*first["items"], *second["items"]]
    ]
    assert customer_ids == sorted(customer_ids)
    assert len(customer_ids) == len(set(customer_ids)) == 30


async def test_profile_write_enforces_permission_origin_validation_concurrency_and_audit(
    customer_workspace,
):
    transport, factory, administrator_id, conversation_id, _other_id, _evidence_id = (
        customer_workspace
    )
    body = {
        "expected": {
            "display_name": "Marie Client",
            "city": "Kinshasa",
            "preferred_language": "french",
        },
        "profile": {
            "display_name": "Marie Updated",
            "city": "Matadi",
            "preferred_language": "lingala",
        },
        "reason_code": "customer_provided_update",
    }
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        operator_csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        assert (
            await client.put(
                f"/api/v1/business/customers/{conversation_id}/profile",
                headers=_headers(operator_csrf),
                json=body,
            )
        ).status_code == 403
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        admin_overview = await client.get("/api/v1/business/customers?q=Marie")
        assert admin_overview.status_code == 200
        assert admin_overview.json()["items"][0]["phone_number"] == "+243810005101"
        path = f"/api/v1/business/customers/{conversation_id}/profile"
        assert (await client.put(path, json=body)).status_code == 403
        assert (
            await client.put(
                path,
                headers=_headers(csrf, origin="https://attacker.example"),
                json=body,
            )
        ).status_code == 403
        invalid = {**body, "profile": {**body["profile"], "city": "   "}}
        assert (
            await client.put(path, headers=_headers(csrf), json=invalid)
        ).status_code == 422
        response = await client.put(path, headers=_headers(csrf), json=body)
        assert response.status_code == 200
        assert response.json()["display_name"] == "Marie Updated"
        assert response.json()["phone_number"] == "+243810005101"
        stale = {**body, "profile": {**body["profile"], "city": "Goma"}}
        assert (
            await client.put(path, headers=_headers(csrf), json=stale)
        ).status_code == 409
    async with factory() as db:
        customer = await db.get(Customer, "+243810005101")
        assert (customer.name, customer.city, customer.preferred_language) == (
            "Marie Updated",
            "Matadi",
            "lingala",
        )
        audits = (
            await db.scalars(
                select(OperatorAuditEvent).where(
                    OperatorAuditEvent.action == "customer.profile.update"
                )
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].actor_account_id == administrator_id
        assert audits[0].target_id == str(conversation_id)
        assert audits[0].event_metadata == {
            "changed_fields": ["display_name", "city", "preferred_language"]
        }


async def test_opt_out_requires_customer_owned_verified_signal_and_is_audited(
    customer_workspace,
):
    transport, factory, administrator_id, conversation_id, other_id, evidence_id = (
        customer_workspace
    )
    async with factory() as db:
        other_evidence = await db.scalar(
            select(Message.message_id)
            .join(Conversation)
            .where(Conversation.conversation_id == other_id)
        )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        path = f"/api/v1/business/customers/{conversation_id}/opt-out"
        payload = {
            "expected_opted_out": False,
            "evidence_message_id": str(other_evidence),
            "attested": True,
        }
        assert (
            await client.post(path, headers=_headers(csrf), json=payload)
        ).status_code == 409
        payload["evidence_message_id"] = str(evidence_id)
        response = await client.post(path, headers=_headers(csrf), json=payload)
        assert response.status_code == 200
        assert response.json()["opted_out"] is True
        replay = await client.post(path, headers=_headers(csrf), json=payload)
        assert replay.status_code == 200
    async with factory() as db:
        customer = await db.get(Customer, "+243810005101")
        assert customer.opt_out_flag is True and customer.opt_out_at is not None
        assert customer.consent_given is False and customer.consent_timestamp is None
        legacy_relance = await db.scalar(
            select(Relance)
            .join(Lead, Lead.lead_id == Relance.lead_id)
            .where(Lead.customer_id == "+243810005101")
        )
        candidate = await db.scalar(
            select(RelanceCandidate)
            .join(Lead, Lead.lead_id == RelanceCandidate.lead_id)
            .where(Lead.customer_id == "+243810005101")
        )
        assert legacy_relance is not None and legacy_relance.cancelled is True
        assert candidate is not None and candidate.cancelled_at is not None
        audits = (
            await db.scalars(
                select(OperatorAuditEvent).where(
                    OperatorAuditEvent.action == "customer.contact.opt_out"
                )
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].actor_account_id == administrator_id
        assert audits[0].event_metadata["evidence_message_id"] == str(evidence_id)
        assert audits[0].event_metadata["relances_cancelled"] == 1
        assert audits[0].event_metadata["relance_candidates_cancelled"] == 1
        assert "+243" not in str(audits[0].event_metadata)
