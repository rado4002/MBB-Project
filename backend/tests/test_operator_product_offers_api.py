"""Real browser-session authorization and read-only Product Offer composition."""

import copy
import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1 import commerce_admin, operator_product_offers
from app.modules.product_offer import service
from app.operator_identity.browser_auth import BrowserAuthState, SESSION_COOKIE_NAME
from test_operator_escalation_api import ORIGIN, FakeDatabase, _account, _app, _settings
from test_product_offer import _inventory, _item, _media, _price, _product, _rate


class ReadOnlyDatabase(FakeDatabase):
    def __init__(self, account):
        super().__init__(account)
        self.rows = [
            service.ProductOfferRow(_product(), _item(), _price(), _inventory("available"), _rate(), _media(suffix="variant")),
            service.ProductOfferRow(_product(), _item(), _price(), _inventory("out_of_stock"), None),
            service.ProductOfferRow(_product(), _item(), _price(), None, None),
            service.ProductOfferRow(_product(), _item(), None, _inventory("unknown"), None),
        ]
        self.statements = []
        self.failure = False

    async def execute(self, statement):
        assert statement.is_select, "Product lookup must only issue reads"
        if self.failure:
            raise SQLAlchemyError("private database details")
        self.statements.append(statement)
        item_id = next((value for value in statement.compile().params.values() if isinstance(value, uuid.UUID)), None)
        if item_id is not None:
            row = next((row for row in self.rows if row.sellable_item.sellable_item_id == item_id), None)
            return SimpleNamespace(one_or_none=lambda: row)
        return SimpleNamespace(all=lambda: self.rows)

    async def commit(self):
        pytest.fail("Product lookup attempted a commit")

    async def flush(self):
        pytest.fail("Product lookup attempted a flush")

    def add(self, _value):
        pytest.fail("Product lookup attempted persistence")


@pytest_asyncio.fixture
async def lookup(monkeypatch):
    settings = _settings()
    account = _account()
    database = ReadOnlyDatabase(account)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = BrowserAuthState(redis_client=redis, settings=settings)
    session = await state.sessions.create_session(
        account_id=account.account_id, auth_version=account.auth_version,
        ip_prefix=state.source_network("127.0.0.1"), user_agent="python-httpx/0.27.2",
    )
    app = _app(settings, redis, database)
    app.include_router(operator_product_offers.router, prefix="/api/v1")
    app.include_router(commerce_admin.router, prefix="/api/v1")

    async def rate(_db, *, at):
        return None

    monkeypatch.setattr(service, "get_active_usd_cdf_rate", rate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url=ORIGIN,
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, session.token)
        yield client, database, account
    await redis.aclose()


@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_human_read_uses_real_offer_composition_without_mutations(lookup, role):
    client, database, account = lookup
    account.role = role
    before = copy.deepcopy(database.rows)
    response = await client.get("/api/v1/operator/product-offers", params={"query": "  Air Fryer  ", "limit": 20})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert [item["offer_status"] for item in items] == ["sellable_now", "out_of_stock", "availability_unconfirmed", "price_unavailable"]
    assert items[0]["current_usd_price"] == "60.00"
    assert items[0]["derived_cdf_quote"]["cdf_amount"] == "168000.00"
    assert items[0]["primary_media"]["source_scope"] == "sellable_item"
    assert items[1]["cdf_quote_status"] == "cdf_quote_unavailable"
    assert items[2]["inventory_status"] == "unknown"
    assert items[3]["current_usd_price"] is None
    statement = database.statements[-1]
    assert "%Air Fryer%" in statement.compile().params.values()
    assert statement._limit_clause.value == 20
    assert "products.active IS true" in str(statement)
    assert "sellable_items.active IS true" in str(statement)
    for item in items:
        detail = await client.get(f'/api/v1/operator/product-offers/{item["sellable_item_id"]}')
        assert detail.status_code == 200
        assert detail.json()["offer_status"] == item["offer_status"]
        assert detail.headers["cache-control"] == "no-store"
    assert database.rows == before


async def test_operator_does_not_gain_maintenance_authority(lookup):
    client, _database, _account_value = lookup
    assert (await client.get("/api/v1/operator/commerce/products")).status_code == 403
    assert (await client.post("/api/v1/operator/commerce/products", json={})).status_code == 403
    assert (await client.post("/api/v1/operator/product-offers", json={})).status_code == 405


@pytest.mark.parametrize("state", ["anonymous", "invalid", "disabled", "analyst", "password_change"])
async def test_unauthorized_read_fails_closed(lookup, state):
    client, database, account = lookup
    if state == "anonymous":
        client.cookies.clear()
    elif state == "invalid":
        client.cookies.set(SESSION_COOKIE_NAME, "invalid")
    elif state == "disabled":
        account.status = "disabled"
    elif state == "analyst":
        account.role = "analyst"
    else:
        account.must_change_password = True
    for path in ["/api/v1/operator/product-offers?query=Air", f"/api/v1/operator/product-offers/{uuid.uuid4()}"]:
        response = await client.get(path)
        assert response.status_code in (401, 403)
        assert response.headers["cache-control"] == "no-store"
    assert not database.statements


@pytest.mark.parametrize("params", [
    {}, {"query": " "}, {"query": "x" * 121}, {"query": "Air", "limit": 0},
    {"query": "Air", "limit": 21}, {"query": "Air", "limit": "1.5"},
    {"query": "Air", "active": "false"},
])
async def test_strict_bounded_search(lookup, params):
    client, database, _account_value = lookup
    response = await client.get("/api/v1/operator/product-offers", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not database.statements


async def test_exact_inactive_missing_and_service_failure(lookup):
    client, database, _account_value = lookup
    row = database.rows[0]
    row.product.active = False
    detail = await client.get(f"/api/v1/operator/product-offers/{row.sellable_item.sellable_item_id}")
    assert detail.json()["offer_status"] == "inactive"
    assert detail.json()["reason_code"] == "product_inactive"
    assert (await client.get(f"/api/v1/operator/product-offers/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/api/v1/operator/product-offers/not-a-uuid")).status_code == 422
    database.failure = True
    for path in ["/api/v1/operator/product-offers?query=Air", f"/api/v1/operator/product-offers/{row.sellable_item.sellable_item_id}"]:
        response = await client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "private database details" not in response.text
