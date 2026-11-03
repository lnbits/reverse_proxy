import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from lnbits.core.models import Payment
from lnbits.db import Database
from lnbits.settings import settings as lnbits_settings
from sqlalchemy import event

from .. import crud, migrations, services
from ..models import (
    CreatePayments,
    CreateProxies,
    CreateTunnelRequest,
    ExtensionSettings,
)


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    monkeypatch.setattr(lnbits_settings, "lnbits_data_folder", str(tmp_path))
    database = Database("ext_reverse_proxy")
    monkeypatch.setattr(crud, "db", database)
    monkeypatch.setattr(services, "db", database)
    for name in sorted(n for n in vars(migrations) if n.startswith("m00")):
        await getattr(migrations, name)(database)
    yield database
    await database.engine.dispose()


@pytest_asyncio.fixture
async def owner(database, tmp_path):
    return await crud.create_extension_settings(
        "owner",
        ExtensionSettings(
            wallet="wallet",
            domain="example.com",
            caddy_tunnels_file=str(tmp_path / "tunnels.caddy"),
            remote_port_min=42000,
            remote_port_max=42010,
        ),
    )


async def reserve(owner, proxy_id="tunnel", port=42000):
    return await crud.create_proxies(
        owner.id,
        CreateProxies(
            subdomain=proxy_id,
            remote_port=port,
            ssh_user="tunneluser",
            ssh_public_key="ssh-ed25519 AAAAoriginal",
            expires_at=datetime.now(timezone.utc),
            status="pending",
            public_id=owner.public_id,
        ),
        proxies_id=proxy_id,
    )


async def invoice(proxy, suffix="one", setup=True):
    await crud.create_payments(
        proxy.id,
        CreatePayments(
            tunnel_id=proxy.id,
            invoice_id=suffix,
            amount=200,
            paid=False,
        ),
        payments_id=suffix,
    )
    extra = {"tag": "reverse_proxy", "days": 2, "payments_id": suffix}
    if setup:
        extra["proxy_setup"] = {
            "payments_id": suffix,
            "proxy_id": proxy.id,
            "pubkey": "ssh-ed25519 AAAAold",
        }
    return Payment(
        checking_id=suffix,
        payment_hash=suffix,
        wallet_id="wallet",
        amount=200000,
        fee=0,
        bolt11="invoice",
        status="success",
        extra=extra,
    )


@pytest.mark.asyncio
async def test_replayed_setup_payment_does_not_extend_or_rotate_key(owner, monkeypatch):
    proxy = await reserve(owner)
    payment = await invoice(proxy)
    sync = AsyncMock()
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", sync)
    assert await services.payment_received_for_payments(payment)
    first = await crud.get_proxies_by_id(proxy.id)
    assert await services.payment_received_for_payments(payment)
    second = await crud.get_proxies_by_id(proxy.id)
    assert second.expires_at == first.expires_at
    assert second.ssh_public_key == "ssh-ed25519 AAAAoriginal"
    assert (await crud.get_payments_by_id("one")).amount == 200
    assert sync.await_count == 2  # Provisioning can be retried after a crash.


@pytest.mark.asyncio
async def test_parallel_invoices_credit_once_each(owner, monkeypatch):
    proxy = await reserve(owner)
    first = await invoice(proxy)
    second = await invoice(proxy, "two", setup=False)
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", AsyncMock())
    start = datetime.now(timezone.utc)
    assert all(
        await asyncio.gather(
            *[services.payment_received_for_payments(payment) for payment in [first, first, second, second]]
        )
    )
    updated = await crud.get_proxies_by_id(proxy.id)
    assert abs((updated.expires_at - start).total_seconds() - 4 * 86400) < 3


@pytest.mark.asyncio
async def test_failure_rolls_back_credit_and_paid_flag(owner, database, monkeypatch):
    proxy = await reserve(owner)
    payment = await invoice(proxy)
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", AsyncMock())

    def fail_expiry(conn, cursor, statement, parameters, context, executemany):
        if "SET status = 'active'" in statement:
            raise RuntimeError("simulated database failure")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail_expiry)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            await services.payment_received_for_payments(payment)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", fail_expiry)
    assert not (await crud.get_payments_by_id("one")).paid
    assert (await crud.get_proxies_by_id(proxy.id)).status == "pending"
    assert await services.payment_received_for_payments(payment)


@pytest.mark.asyncio
async def test_provisioning_failure_can_retry_without_double_credit(owner, monkeypatch):
    proxy = await reserve(owner)
    payment = await invoice(proxy)
    sync = AsyncMock(side_effect=OSError("Caddy unavailable"))
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", sync)
    with pytest.raises(OSError):
        await services.payment_received_for_payments(payment)
    expiry = (await crud.get_proxies_by_id(proxy.id)).expires_at
    sync.side_effect = None
    assert await services.payment_received_for_payments(payment)
    assert (await crud.get_proxies_by_id(proxy.id)).expires_at == expiry


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"status": "pending"}, {"wallet_id": "other"}, {"amount": 1}])
async def test_unpaid_wrong_wallet_or_wrong_amount_cannot_activate(owner, monkeypatch, change):
    proxy = await reserve(owner)
    payment = (await invoice(proxy)).model_copy(update=change)
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", AsyncMock())
    assert not await services.payment_received_for_payments(payment)
    assert (await crud.get_proxies_by_id(proxy.id)).status == "pending"


@pytest.mark.asyncio
async def test_repeat_setup_does_not_rekey_existing_tunnel(owner, monkeypatch):
    monkeypatch.setattr(services, "generate_ssh_keypair", lambda _: ("private", "ssh-ed25519 AAAAnew"))

    async def new_invoice(**kwargs):
        suffix = str(new_invoice.count)
        new_invoice.count += 1
        return Payment(
            checking_id=suffix,
            payment_hash=suffix,
            wallet_id="wallet",
            amount=100000,
            fee=0,
            bolt11="invoice",
            extra=kwargs["extra"],
        )

    new_invoice.count = 0
    monkeypatch.setattr(services, "create_invoice", new_invoice)
    request = CreateTunnelRequest(public_id=owner.public_id, days=1, client_note="same-client")
    first = await services.create_tunnel_with_invoice(request, owner)
    second = await services.create_tunnel_with_invoice(request, owner)
    assert first.tunnel_id != second.tunnel_id
    assert first.remote_port != second.remote_port
    assert first.subdomain != second.subdomain


@pytest.mark.asyncio
async def test_port_range_is_inclusive_and_exhaustion_fails_promptly(database):
    assert await services.allocate_remote_port(42999, 42999) == 42999
    with pytest.raises(ValueError, match="Invalid"):
        await services.allocate_remote_port(43000, 42999)
    original = services._get_used_ports
    services._get_used_ports = AsyncMock(return_value={42999})
    try:
        with pytest.raises(ValueError, match="No remote ports"):
            await asyncio.wait_for(services.allocate_remote_port(42999, 42999), timeout=1)
    finally:
        services._get_used_ports = original


@pytest.mark.asyncio
async def test_shared_caddy_file_preserves_all_owners(owner, monkeypatch):
    other = await crud.create_extension_settings(
        "other",
        ExtensionSettings(**{**owner.model_dump(exclude={"id"}), "domain": "other.example"}),
    )
    first = await reserve(owner)
    second = await reserve(other, "second", 42001)
    for proxy in [first, second]:
        proxy.status = "active"
        proxy.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        await crud.update_proxies(proxy)
    monkeypatch.setattr(services, "_add_authorized_key", lambda *args: None)
    monkeypatch.setattr(services.subprocess, "run", lambda *args, **kwargs: None)
    await services.cleanup_expired_proxies_and_sync_caddy(owner.id, owner)
    from pathlib import Path

    content = Path(owner.caddy_tunnels_file).read_text()
    assert "tunnel.example.com" in content
    assert "second.other.example" in content


@pytest.mark.asyncio
async def test_status_is_invoice_scoped_and_recovers_missed_event(owner, monkeypatch):
    proxy = await reserve(owner)
    payment = await invoice(proxy)
    monkeypatch.setattr(services, "get_standalone_payment", AsyncMock(return_value=payment))
    monkeypatch.setattr(services, "cleanup_expired_proxies_and_sync_caddy", AsyncMock())
    result = await services.tunnel_payment_status(proxy.id, payment.payment_hash)
    assert result["paid"] and result["ready"]
    other = await reserve(owner, "other", 42001)
    assert await services.tunnel_payment_status(other.id, payment.payment_hash) is None


@pytest.mark.asyncio
async def test_expiry_cleanup_cannot_overwrite_concurrent_renewal(owner, database, monkeypatch):
    proxy = await reserve(owner)
    proxy.status = "active"
    proxy.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await crud.update_proxies(proxy)
    original = database.fetchall
    renewed_expiry = datetime.now(timezone.utc) + timedelta(days=2)

    async def renew_after_read(*args, **kwargs):
        stale_rows = await original(*args, **kwargs)
        proxy.expires_at = renewed_expiry
        await crud.update_proxies(proxy)
        return stale_rows

    monkeypatch.setattr(database, "fetchall", renew_after_read)
    remove = AsyncMock()
    monkeypatch.setattr(services, "_remove_authorized_key", remove)
    await services._mark_expired_proxies(owner.id, owner, datetime.now(timezone.utc))
    updated = await crud.get_proxies_by_id(proxy.id)
    assert updated.status == "active"
    assert abs((updated.expires_at - renewed_expiry).total_seconds()) < 1
    remove.assert_not_called()


@pytest.mark.asyncio
async def test_management_updates_preserve_model_fields(owner):
    from lnbits.core.models import User

    from ..views_api import api_update_payments, api_update_proxies

    now = datetime.now(timezone.utc)
    user = User(id=owner.id, created_at=now, updated_at=now, super_user=True)
    settings_data = ExtensionSettings.model_validate(owner.model_dump())
    settings_data.price_per_day_sats = 150
    await crud.update_extension_settings(owner.id, settings_data)
    assert (await crud.get_extension_settings(owner.id)).public_id == owner.public_id
    assert (await crud.get_extension_settings(owner.id)).price_per_day_sats == 150

    original = await reserve(owner)
    proxy_data = CreateProxies.model_validate(original.model_dump())
    proxy_data.client_note = "updated note"
    updated = await api_update_proxies(original.id, proxy_data, user)
    persisted = await crud.get_proxies_by_id(original.id)
    assert persisted.client_note == "updated note"
    assert persisted.id == original.id
    assert updated.created_at == persisted.created_at
    assert persisted.expires_at.tzinfo is not None

    await invoice(persisted)
    payment_data = CreatePayments(tunnel_id=original.id, invoice_id="one", amount=300, paid=False)
    edited = await api_update_payments("one", payment_data, user)
    stored = await crud.get_payments_by_id("one")
    assert stored.id == edited.id == "one"
    assert stored.proxies_id == original.id
    assert stored.amount == 300
