"""
app/adapters/crm — CRM adapter factory.

Usage:
    from app.adapters.crm import get_crm_adapter
    adapter = get_crm_adapter()
    record_id = await adapter.sync_order(order_data)

Switching: CRM_ADAPTER=airtable (default). Future: CRM_ADAPTER=mbb_hub.
"""
from __future__ import annotations

from app.adapters.base import BaseCRMAdapter

_instance: BaseCRMAdapter | None = None


def get_crm_adapter() -> BaseCRMAdapter:
    """Return the configured CRM adapter singleton."""
    global _instance
    if _instance is None:
        from app.adapters.crm.airtable_adapter import AirtableAdapter
        _instance = AirtableAdapter()
    return _instance
