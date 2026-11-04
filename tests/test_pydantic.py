import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from lnbits.core.models import Account, User
from lnbits.decorators import check_account_exists, check_user_exists
from lnbits.settings import settings

from .. import views
from ..models import ExtensionSettings, PaymentsFilters, ProxiesFilters


@pytest.mark.parametrize("model", [ProxiesFilters, PaymentsFilters])
def test_filter_dates_can_be_omitted(model):
    filters = model.model_validate({})
    assert filters.created_at is None
    assert filters.updated_at is None
    assert model.model_json_schema().get("required", []) == []


def test_initial_settings_allow_missing_public_id():
    settings = ExtensionSettings.model_validate({})
    assert settings.public_id is None
    assert settings.wallet == ""
    assert isinstance(settings.model_dump()["updated_at"], datetime)


@pytest.mark.asyncio
async def test_frontend_serializes_user_for_template(monkeypatch):
    now = datetime.now(timezone.utc)
    user = User(id="owner", created_at=now, updated_at=now, super_user=True)
    request = Request({"type": "http", "method": "GET", "path": "/"})
    renderer = SimpleNamespace(TemplateResponse=lambda name, context: context)
    monkeypatch.setattr(views, "reverse_proxy_renderer", lambda: renderer)
    monkeypatch.setattr(views, "get_settings", AsyncMock(return_value=ExtensionSettings()))
    monkeypatch.setattr(views, "cleanup_expired_proxies_and_sync_caddy", AsyncMock())
    context = await views.index(request, user)
    assert json.loads(context["user"])["id"] == user.id
    assert User.model_validate_json(context["user"]).created_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize("is_super_user", [True, False])
async def test_page_dependencies_supply_full_user_and_enforce_superuser(monkeypatch, is_super_user):
    monkeypatch.setattr(settings, "super_user", "owner")
    monkeypatch.setattr(settings, "lnbits_admin_users", [])
    account = Account(id="owner" if is_super_user else "other", password_hash="test-only")
    user = User(id=account.id, super_user=is_super_user, created_at=account.created_at, updated_at=account.updated_at)
    app = FastAPI()
    app.include_router(views.reverse_proxy_generic_router)
    # Preserve the real superuser guard, including its Account return type.
    app.dependency_overrides[check_account_exists] = lambda: account
    app.dependency_overrides[check_user_exists] = lambda: user
    renderer = SimpleNamespace(TemplateResponse=lambda name, context: JSONResponse(json.loads(context["user"])))
    monkeypatch.setattr(views, "reverse_proxy_renderer", lambda: renderer)
    monkeypatch.setattr(views, "get_settings", AsyncMock(return_value=ExtensionSettings()))
    cleanup = AsyncMock()
    monkeypatch.setattr(views, "cleanup_expired_proxies_and_sync_caddy", cleanup)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    if not is_super_user:
        assert response.status_code == 403
        cleanup.assert_not_awaited()
        return
    assert response.status_code == 200
    payload = response.json()
    assert payload["wallets"] == []
    assert payload["extensions"] == []
    assert payload["super_user"] is True
    assert "password_hash" not in payload
