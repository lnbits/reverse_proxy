"""Both extension APIs over HTTP, with real databases and reverse SSH forwarding.

Requires the sibling tunnel_me_out checkout. Lightning settlement and the Caddy
service reload are simulated; no external server or real wallet is contacted.
"""

import asyncio
import getpass
import importlib
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from lnbits.core.models import Payment
from lnbits.db import Database
from lnbits.decorators import check_super_user
from lnbits.settings import settings as lnbits_settings

from .. import crud, migrations, services
from ..models import ExtensionSettings
from ..views_api import reverse_proxy_api_router


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def http_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if task.done():
                await task
                raise RuntimeError("HTTP server stopped before startup")
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("HTTP server startup timed out")
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            sock.close()


@asynccontextmanager
async def ssh_server(directory):
    executable = shutil.which("sshd")
    if not executable:
        pytest.skip("OpenSSH server is not installed")
    port = free_port()
    key = directory / "host_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    config = directory / "sshd_config"
    config.write_text(
        f"""Port {port}
ListenAddress 127.0.0.1
HostKey {key}
PidFile {directory / 'sshd.pid'}
AuthorizedKeysFile {directory / 'authorized_keys'}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding remote
AllowStreamLocalForwarding no
GatewayPorts no
"""
    )
    with open(directory / "sshd.log", "w+") as log:
        daemon = await asyncio.create_subprocess_exec(executable, "-D", "-e", "-f", str(config), stderr=log)
        try:
            for _ in range(100):
                if daemon.returncode is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.02)
            else:
                raise RuntimeError("SSH server startup timed out")
            yield port
        finally:
            if daemon.returncode is None:
                daemon.terminate()
            await asyncio.wait_for(daemon.wait(), timeout=5)


class FakeInvoices:
    """Only the Lightning boundary is replaced; extension settlement is real."""

    def __init__(self):
        self.payments = {}

    async def create(self, wallet_id, amount, extra, memo):
        payment_hash = f"{len(self.payments) + 1:064x}"
        payment = Payment(
            checking_id=payment_hash,
            payment_hash=payment_hash,
            wallet_id=wallet_id,
            amount=amount * 1000,
            fee=0,
            bolt11=f"test-invoice-{payment_hash}",
            memo=memo,
            extra=extra,
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.payments[payment_hash] = payment
        return payment

    async def get(self, payment_hash, incoming=False, wallet_id=None):
        payment = self.payments.get(payment_hash)
        if payment and (not wallet_id or payment.wallet_id == wallet_id):
            return payment
        return None

    def settle(self, payment_hash):
        self.payments[payment_hash].status = "success"


async def migrate(database, module):
    for name in sorted(name for name in vars(module) if name.startswith("m00")):
        await getattr(module, name)(database)


async def client_record(http, base):
    response = await http.get(f"{base}/tunnel_me_out/api/v1/tunnel")
    response.raise_for_status()
    return response.json()["tunnel"]


@pytest.mark.asyncio
async def test_paired_request_payment_forward_renew_restart_expire(tmp_path, monkeypatch):
    sibling = Path(__file__).resolve().parents[2] / "tunnel_me_out"
    if not sibling.is_dir():
        pytest.skip("Run alongside the tunnel_me_out checkout")
    monkeypatch.syspath_prepend(str(sibling.parent))
    client = importlib.import_module("tunnel_me_out.services")
    client_crud = importlib.import_module("tunnel_me_out.crud")
    client_migrations = importlib.import_module("tunnel_me_out.migrations")
    client_views = importlib.import_module("tunnel_me_out.views_api")

    monkeypatch.setattr(lnbits_settings, "lnbits_data_folder", str(tmp_path))
    server_db = Database("ext_reverse_proxy")
    client_db = Database("ext_tunnel_me_out")
    monkeypatch.setattr(crud, "db", server_db)
    monkeypatch.setattr(services, "db", server_db)
    monkeypatch.setattr(client_crud, "db", client_db)
    monkeypatch.setattr(services, "_sync_lock", asyncio.Lock())
    monkeypatch.setattr(client, "_lifecycle_lock", asyncio.Lock())
    monkeypatch.setattr(services, "_reloaded_files", {})
    monkeypatch.setattr(services, "_authorized_keys_path", lambda _: str(tmp_path / "authorized_keys"))
    expanduser = client.os.path.expanduser
    monkeypatch.setattr(
        client.os.path, "expanduser", lambda value: str(tmp_path / "client-home") if value == "~" else expanduser(value)
    )
    invoices = FakeInvoices()
    monkeypatch.setattr(services, "create_invoice", invoices.create)
    monkeypatch.setattr(services, "get_standalone_payment", invoices.get)
    original_run = subprocess.run
    reloads = []

    def simulate_caddy_reload(args, **kwargs):
        if args == ["sudo", "-n", "systemctl", "reload", "caddy"]:
            reloads.append(args)
            return subprocess.CompletedProcess(args, 0)
        return original_run(args, **kwargs)

    monkeypatch.setattr(services.subprocess, "run", simulate_caddy_reload)
    try:
        await migrate(server_db, migrations)
        await migrate(client_db, client_migrations)
        remote_port = free_port()
        caddy_file = tmp_path / "tunnels.caddy"
        owner = await crud.create_extension_settings(
            "server-owner",
            ExtensionSettings(
                wallet="test-wallet",
                domain="example.test",
                ssh_user=getpass.getuser(),
                ssh_host="127.0.0.1",
                caddy_tunnels_file=str(caddy_file),
                remote_port_min=remote_port,
                remote_port_max=remote_port,
            ),
        )
        server_app = FastAPI()
        server_app.include_router(reverse_proxy_api_router, prefix="/reverse_proxy")
        client_app = FastAPI()
        client_app.include_router(client_views.tunnel_router, prefix="/tunnel_me_out")
        client_app.dependency_overrides[check_super_user] = lambda: SimpleNamespace(id="client-owner")

        @client_app.get("/probe")
        async def probe():
            return {"message": "through the actual SSH tunnel"}

        async with (
            ssh_server(tmp_path) as ssh_port,
            http_server(server_app) as (server_url, _),
            http_server(client_app) as (client_url, local_port),
        ):
            monkeypatch.setattr(client, "REMOTE_BASE", server_url)
            monkeypatch.setattr(client, "REMOTE_PUBLIC_ID", owner.public_id)
            original_command = client._ssh_command
            monkeypatch.setattr(
                client,
                "_ssh_command",
                lambda *args: [*original_command(*args)[:-1], "-p", str(ssh_port), original_command(*args)[-1]],
            )
            async with httpx.AsyncClient(timeout=15) as http:
                purchase = await http.post(
                    f"{client_url}/tunnel_me_out/api/v1/tunnel",
                    json={
                        "days": 2,
                        "local_host": "127.0.0.1",
                        "local_port": local_port,
                    },
                )
                purchase.raise_for_status()
                pending = purchase.json()
                assert pending["status"] == "pending"
                tunnel_id, first_hash = pending["tunnel_id"], pending["payment_hash"]
                assert (await client_record(http, client_url))["ssh_private_key"] == pending["ssh_private_key"]
                duplicate = await http.post(f"{client_url}/tunnel_me_out/api/v1/tunnel", json={"days": 2})
                duplicate.raise_for_status()
                assert duplicate.json()["payment_hash"] == first_hash
                assert len(invoices.payments) == 1

                # No payment callback or WebSocket: polling must recover settlement.
                invoices.settle(first_hash)
                await asyncio.wait_for(asyncio.shield(client._payment_watchers[first_hash]), timeout=12)
                active = await client_record(http, client_url)
                assert active["status"] == "active" and active["last_error"] is None
                first_process = client._ssh_processes[tunnel_id]
                traffic = await http.get(f"http://127.0.0.1:{remote_port}/probe")
                assert traffic.json() == {"message": "through the actual SSH tunnel"}
                assert f"reverse_proxy 127.0.0.1:{remote_port}" in caddy_file.read_text()
                assert reloads

                reconnect = await http.post(f"{client_url}/tunnel_me_out/api/v1/tunnel/reconnect")
                reconnect.raise_for_status()
                assert reconnect.json()["expires_at"] == active["expires_at"]
                assert client._ssh_processes[tunnel_id] is first_process

                renewal = await http.post(f"{client_url}/tunnel_me_out/api/v1/tunnel", json={"days": 3})
                renewal.raise_for_status()
                second = renewal.json()
                assert second["tunnel_id"] == tunnel_id
                assert second["ssh_private_key"] == pending["ssh_private_key"]
                assert client._ssh_processes[tunnel_id] is first_process
                invoices.settle(second["payment_hash"])
                await asyncio.wait_for(asyncio.shield(client._payment_watchers[second["payment_hash"]]), timeout=12)
                renewed = await client_record(http, client_url)
                expiry = datetime.fromisoformat(renewed["expires_at"])
                assert expiry - datetime.fromisoformat(active["expires_at"]) == timedelta(days=3)
                await services.payment_received_for_payments(invoices.payments[second["payment_hash"]])
                assert (await crud.get_proxies_by_id(tunnel_id)).expires_at == expiry

                # Rehydrate from the persisted client DB after stopping all tasks/processes.
                await client.shutdown()
                assert first_process.returncode is not None
                restored = await client.activate_tunnel("client-owner")
                assert restored.expires_at == expiry and restored.last_error is None
                assert (await http.get(f"http://127.0.0.1:{remote_port}/probe")).status_code == 200

                proxy = await crud.get_proxies_by_id(tunnel_id)
                proxy.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                await crud.update_proxies(proxy)
                await services.cleanup_expired_proxies_and_sync_caddy(owner.id, owner)
                expired = await client.activate_tunnel("client-owner")
                assert expired.status == "expired"
                assert tunnel_id not in client._ssh_processes
                assert caddy_file.read_text() == ""
                assert f"reverse_proxy:{tunnel_id}" not in (tmp_path / "authorized_keys").read_text()
    finally:
        await client.shutdown()
        await server_db.engine.dispose()
        await client_db.engine.dispose()
