from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import messages
from app.api.v1.messages import BaileysWebhookPayload, OutboundSendRequest
from app.models.customer import Customer
from app.modules.m1_gateway.normalizer import (
    normalize_baileys_webhook,
    normalize_official_webhook,
)
from app.schemas.common import ContentType
from app.schemas.messages import InboundMessageRequest, QueuedMessageResponse


ACCEPTED_PHONES = (
    "+243996123456",
    "+8613812345678",
    "+4915123456789",
    "+12025550123",
)
REJECTED_PHONES = (
    "243996123456",
    "+0123456789",
    "+1",
    "+1234567890123456",
    "+49 151 23456789",
    "+49-151-23456789",
)


def _inbound_values(phone: str) -> dict:
    return {
        "message_id": uuid.uuid4(),
        "customer_phone": phone,
        "content": "test",
        "content_type": ContentType.text,
        "timestamp": datetime.now(timezone.utc),
        "whatsapp_message_id": "test-message-id",
    }


class InternationalPhoneSchemaTests(unittest.TestCase):
    def test_inbound_and_baileys_schemas_accept_canonical_e164_unchanged(self):
        for phone in ACCEPTED_PHONES:
            with self.subTest(phone=phone):
                inbound = InboundMessageRequest(**_inbound_values(phone))
                baileys = BaileysWebhookPayload(**_inbound_values(phone))
                outbound = OutboundSendRequest(customer_phone=phone, text="test")

                self.assertEqual(inbound.customer_phone, phone)
                self.assertEqual(baileys.customer_phone, phone)
                self.assertEqual(baileys.to_inbound_request().customer_phone, phone)
                self.assertEqual(outbound.customer_phone, phone)

    def test_inbound_and_baileys_schemas_reject_noncanonical_phone(self):
        for phone in REJECTED_PHONES:
            with self.subTest(phone=phone):
                with self.assertRaises(ValidationError):
                    InboundMessageRequest(**_inbound_values(phone))
                with self.assertRaises(ValidationError):
                    BaileysWebhookPayload(**_inbound_values(phone))
                with self.assertRaises(ValidationError):
                    OutboundSendRequest(customer_phone=phone, text="test")

    def test_webhook_normalizers_preserve_country_code_without_guessing(self):
        baileys = normalize_baileys_webhook({
            "key": {
                "remoteJid": "8613812345678@s.whatsapp.net",
                "id": "test-message-id",
            },
            "message": {"conversation": "test"},
            "messageTimestamp": 1714182000,
        })
        official = normalize_official_webhook({
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "4915123456789",
                            "id": "test-message-id",
                            "timestamp": "1714182000",
                            "type": "text",
                            "text": {"body": "test"},
                        }],
                    },
                }],
            }],
        })

        self.assertEqual(baileys["customer_phone"], "+8613812345678")
        self.assertEqual(official["customer_phone"], "+4915123456789")

    def test_webhook_normalizers_reject_malformed_phone(self):
        for phone in ("49 151 23456789", "49-151-23456789"):
            with self.subTest(phone=phone):
                with self.assertRaises(ValueError):
                    normalize_baileys_webhook({
                        "key": {
                            "remoteJid": f"{phone}@s.whatsapp.net",
                            "id": "test-message-id",
                        },
                        "message": {"conversation": "test"},
                        "messageTimestamp": 1714182000,
                    })

    def test_baileys_webhook_accepts_canonical_e164(self):
        app = FastAPI()
        app.include_router(messages.router, prefix="/api/v1")
        client = TestClient(app)
        queued = QueuedMessageResponse(
            queue_position=1,
            estimated_processing_seconds=10,
        )

        for phone in ACCEPTED_PHONES:
            with (
                self.subTest(phone=phone),
                patch.object(messages.settings, "whatsapp_mode", "baileys"),
                patch.object(messages.settings, "baileys_webhook_secret", "test-secret"),
                patch.object(
                    messages,
                    "_handle_inbound",
                    AsyncMock(return_value=queued),
                ) as handle,
            ):
                response = client.post(
                    "/api/v1/messages/baileys",
                    headers={"X-Webhook-Secret": "test-secret"},
                    json={
                        "customer_phone": phone,
                        "content": "test",
                        "content_type": "text",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "whatsapp_message_id": "test-message-id",
                    },
                )

                self.assertEqual(response.status_code, 202)
                self.assertEqual(
                    handle.await_args.kwargs["payload"].customer_phone,
                    phone,
                )

    def test_baileys_webhook_rejects_noncanonical_phone_at_customer_field(self):
        app = FastAPI()
        app.include_router(messages.router, prefix="/api/v1")
        client = TestClient(app)

        for phone in REJECTED_PHONES:
            with self.subTest(phone=phone):
                response = client.post(
                    "/api/v1/messages/baileys",
                    json={
                        "customer_phone": phone,
                        "content": "test",
                        "content_type": "text",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "whatsapp_message_id": "test-message-id",
                    },
                )

                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["detail"][0]["loc"],
                    ["body", "customer_phone"],
                )

    def test_customer_model_uses_international_e164_constraint(self):
        constraint = next(
            constraint
            for constraint in Customer.__table__.constraints
            if constraint.name == "chk_phone_format"
        )
        self.assertEqual(
            str(constraint.sqltext),
            r"phone_number ~ '^\+[1-9][0-9]{6,14}$'",
        )


if __name__ == "__main__":
    unittest.main()
