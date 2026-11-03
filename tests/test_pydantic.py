import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from lnbits.core.models import User

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
