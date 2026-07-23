"""
Adapter base interfaces.

Rule: All modules (M1–M9) call adapters only through these interfaces.
      Never import an adapter implementation directly from a module.
      Switch adapters via environment variables — zero module code changes.
"""
from abc import ABC, abstractmethod
from typing import Any


class BaseAIAdapter(ABC):
    @abstractmethod
    async def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        """Generate a response from the AI model."""

    @abstractmethod
    async def detect_language(self, text: str) -> str:
        """Return detected language: 'lingala' | 'french' | 'swahili'."""


class BaseCRMAdapter(ABC):
    @abstractmethod
    async def create_lead(self, lead_data: dict[str, Any]) -> str:
        """Create a lead record. Returns external CRM record ID."""

    @abstractmethod
    async def update_lead(self, external_id: str, updates: dict[str, Any]) -> None:
        """Update a lead record in the CRM."""

    @abstractmethod
    async def sync_order(self, order_data: dict[str, Any]) -> str:
        """Sync a confirmed order. Returns external CRM record ID."""


class BaseInventoryAdapter(ABC):
    @abstractmethod
    async def get_product(self, product_id: str) -> dict[str, Any] | None:
        """Return product details or None if not found."""

    @abstractmethod
    async def list_products(self, category: str | None = None) -> list[dict[str, Any]]:
        """Return available products, optionally filtered by category."""


class BasePaymentAdapter(ABC):
    @abstractmethod
    async def initiate_payment(
        self, phone: str, amount: float, currency: str, reference: str, method: str
    ) -> dict[str, Any]:
        """Initiate a mobile money payment. Returns provider response."""

    @abstractmethod
    async def verify_payment(self, transaction_id: str) -> dict[str, Any]:
        """Verify payment status with provider."""


class BaseMessagingAdapter(ABC):
    @abstractmethod
    async def send_message(
        self,
        phone: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Send a text message. Returns provider message ID."""

    @abstractmethod
    async def send_template(
        self, phone: str, template_name: str, params: list[str]
    ) -> str:
        """Send a pre-approved template message."""
