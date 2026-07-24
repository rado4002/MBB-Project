import unittest
from unittest.mock import patch

from fastapi import HTTPException, status
from starlette.requests import Request

from app.adapters.messaging import whatsapp_official_adapter
from app.api.v1 import messages


class OfficialWebhookAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_verification_fails_closed_without_configured_token(self):
        with patch.object(messages.settings, "whatsapp_verify_token", ""):
            with self.assertRaises(HTTPException) as raised:
                await messages.verify_webhook("subscribe", "challenge", "")

        self.assertEqual(raised.exception.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(raised.exception.detail, "webhook_verify_token_not_configured")

    async def test_verification_requires_matching_configured_token(self):
        with patch.object(messages.settings, "whatsapp_verify_token", "fake-verify-token"):
            challenge = await messages.verify_webhook(
                "subscribe",
                "validation-challenge",
                "fake-verify-token",
            )
            self.assertEqual(challenge, "validation-challenge")

            with self.assertRaises(HTTPException) as raised:
                await messages.verify_webhook("subscribe", "challenge", "wrong-token")

        self.assertEqual(raised.exception.status_code, status.HTTP_403_FORBIDDEN)

    async def test_official_webhook_fails_closed_without_api_secret(self):
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        request = Request(scope, receive)

        with patch.object(messages.settings, "whatsapp_api_secret", ""):
            with self.assertRaises(HTTPException) as raised:
                await messages.receive_webhook(
                    request,
                    x_hub_signature_256="sha256=fake-signature",
                    x_webhook_secret=None,
                )

        self.assertEqual(raised.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(raised.exception.detail, "webhook_secret_not_configured")

    async def test_official_webhook_rejects_invalid_hmac_with_configured_secret(self):
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        request = Request(scope, receive)

        with patch.object(messages.settings, "whatsapp_api_secret", "fake-api-secret"):
            with self.assertRaises(HTTPException) as raised:
                await messages.receive_webhook(
                    request,
                    x_hub_signature_256="sha256=invalid-signature",
                    x_webhook_secret=None,
                )

        self.assertEqual(raised.exception.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(raised.exception.detail, "invalid_signature")

    async def test_official_send_gate_returns_before_http_client_creation(self):
        adapter = whatsapp_official_adapter.WhatsAppOfficialAdapter()

        with (
            patch.object(whatsapp_official_adapter.settings, "whatsapp_send_enabled", False),
            patch.object(
                whatsapp_official_adapter.httpx,
                "AsyncClient",
                side_effect=AssertionError("external HTTP client must not be constructed"),
            ) as client_factory,
        ):
            message_id = await adapter.send_message("+243990000000", "validation")
            template_id = await adapter.send_template(
                "+243990000000",
                "validation-template",
                ["validation"],
            )

        self.assertEqual(message_id, "")
        self.assertEqual(template_id, "")
        client_factory.assert_not_called()
