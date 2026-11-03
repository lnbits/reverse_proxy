"""Real loopback OpenSSH smoke test; skipped if the host has no sshd binary."""

import asyncio
import getpass
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from .. import services
from ..models import ExtensionSettings, Proxies


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_host", ["127.0.0.1:", ""])
async def test_restricted_key_forwards_only_reserved_port(tmp_path, monkeypatch, bind_host):  # noqa: C901
    sshd = shutil.which("sshd")
    if not sshd:
        pytest.skip("OpenSSH server is not installed")
    ssh_port, remote_port, denied_port = free_port(), free_port(), free_port()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    for path in [host_key, client_key]:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    authorized = tmp_path / "authorized_keys"
    monkeypatch.setattr(services, "_authorized_keys_path", lambda _: str(authorized))
    proxy = Proxies(
        id="integration",
        user_id="owner",
        subdomain="integration",
        remote_port=remote_port,
        ssh_user=getpass.getuser(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        status="active",
        public_id="public",
    )
    services._add_authorized_key(ExtensionSettings(), proxy, client_key.with_suffix(".pub").read_text())
    config = tmp_path / "sshd_config"
    config.write_text(
        f"""Port {ssh_port}
ListenAddress 127.0.0.1
HostKey {host_key}
PidFile {tmp_path / 'sshd.pid'}
AuthorizedKeysFile {authorized}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding yes
AllowStreamLocalForwarding no
GatewayPorts no
LogLevel VERBOSE
"""
    )
    server_log = open(tmp_path / "sshd.log", "w+")
    server = await asyncio.create_subprocess_exec(sshd, "-D", "-e", "-f", str(config), stderr=server_log)
    client = None
    local_server = None
    try:
        for _ in range(50):
            if server.returncode is not None:
                server_log.seek(0)
                pytest.fail(f"Isolated sshd could not start: {server_log.read()}")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", ssh_port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

        async def echo(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        local_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        local_port = local_server.sockets[0].getsockname()[1]
        common = [
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            str(client_key),
            "-p",
            str(ssh_port),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-T",
        ]
        destination = f"{getpass.getuser()}@127.0.0.1"
        client = await asyncio.create_subprocess_exec(
            *common,
            "-R",
            f"{bind_host}{remote_port}:127.0.0.1:{local_port}",
            destination,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(100):
            if client.returncode is not None:
                pytest.fail((await client.stderr.read()).decode())
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", remote_port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("Remote listener did not start")
        writer.write(b"tunnel works")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), timeout=5) == b"tunnel works"
        writer.close()
        await writer.wait_closed()
        denied = await asyncio.create_subprocess_exec(
            *common,
            "-R",
            f"127.0.0.1:{denied_port}:127.0.0.1:{local_port}",
            destination,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(denied.communicate(), timeout=5)
            assert denied.returncode != 0
            assert b"remote port forwarding failed" in stderr
        finally:
            if denied.returncode is None:
                denied.kill()
                await denied.wait()
    finally:
        for process in [client, server]:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
        if local_server:
            local_server.close()
            await local_server.wait_closed()
        server_log.close()
