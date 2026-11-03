from datetime import datetime, timedelta, timezone

from ..models import ExtensionSettings, Proxies
from ..services import (
    _add_authorized_key,
    _extract_authorized_key,
    _normalize_public_key,
    _remove_existing_caddy_block,
    _select_canonical_proxies,
)


def _proxy(proxy_id: str, subdomain: str, remote_port: int, *, expires_offset_days: int = 1) -> Proxies:
    now = datetime.now(timezone.utc)
    return Proxies(
        id=proxy_id,
        user_id="user-1",
        subdomain=subdomain,
        remote_port=remote_port,
        ssh_user="tunneluser",
        ssh_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey comment",
        expires_at=now + timedelta(days=expires_offset_days),
        status="active",
        public_id="public-1",
        client_note="note",
        created_at=now,
        updated_at=now + timedelta(minutes=remote_port),
    )


def test_select_canonical_proxies_dedupes_hosts():
    proxies = [
        _proxy("old", "same-host", 12000, expires_offset_days=1),
        _proxy("new", "same-host", 12001, expires_offset_days=3),
        _proxy("other", "other-host", 12002, expires_offset_days=2),
    ]

    selected = _select_canonical_proxies(proxies, "example.com")

    assert len(selected) == 2
    assert {proxy.id for proxy in selected} == {"new", "other"}


def test_remove_existing_caddy_block_removes_single_line_and_multiline(tmp_path):
    caddy_file = tmp_path / "tunnels.caddy"
    caddy_file.write_text(
        (
            "dup.example.com { reverse_proxy 127.0.0.1:12000 }\n"
            "keep.example.com {\n"
            "  reverse_proxy 127.0.0.1:12001\n"
            "}\n"
            "dup.example.com {\n"
            "  reverse_proxy 127.0.0.1:12002\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    _remove_existing_caddy_block(str(caddy_file), "dup.example.com")

    remaining = caddy_file.read_text(encoding="utf-8")
    assert "dup.example.com" not in remaining
    assert "keep.example.com" in remaining


def test_add_authorized_key_replaces_existing_marker(tmp_path, monkeypatch):
    ssh_dir = tmp_path / "home" / "tunneluser" / ".ssh"
    ssh_dir.mkdir(parents=True)
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_text(
        (
            'no-pty,permitopen="127.0.0.1:12000",command="echo reverse_proxy:proxy-1" '
            "ssh-ed25519 AAAAOLD reverse_proxy:proxy-1\n"
        ),
        encoding="utf-8",
    )

    from .. import services

    monkeypatch.setattr(services, "_authorized_keys_path", lambda settings: str(authorized_keys))

    settings = ExtensionSettings(ssh_user="tunneluser")
    proxy = _proxy("proxy-1", "same-host", 12001)
    _add_authorized_key(settings, proxy, "ssh-ed25519 AAAANEW")

    content = authorized_keys.read_text(encoding="utf-8")
    assert "AAAAOLD" not in content
    assert "AAAANEW" in content
    assert 'permitlisten="127.0.0.1:12001"' in content
    assert 'permitlisten="localhost:12001"' in content


def test_normalize_public_key_recovers_from_corrupted_authorized_keys_line():
    corrupted = (
        "no-pty,no-user-rc,no-X11-forwarding,no-agent-forwarding,"
        'permitopen="127.0.0.1:10003",command="echo reverse_proxy:proxy-1" '
        'reverse_proxy:proxy-1" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey '
        "reverse_proxy proxy-1 reverse_proxy:proxy-1"
    )

    assert _normalize_public_key(corrupted) == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"


def test_extract_authorized_key_reads_corrupted_line(tmp_path, monkeypatch):
    ssh_dir = tmp_path / "home" / "tunneluser" / ".ssh"
    ssh_dir.mkdir(parents=True)
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_text(
        (
            "no-pty,no-user-rc,no-X11-forwarding,no-agent-forwarding,"
            'permitopen="127.0.0.1:10003",command="echo reverse_proxy:proxy-1" '
            'reverse_proxy:proxy-1" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey '
            "reverse_proxy proxy-1 reverse_proxy:proxy-1\n"
        ),
        encoding="utf-8",
    )

    from .. import services

    monkeypatch.setattr(services, "_authorized_keys_path", lambda settings: str(authorized_keys))

    settings = ExtensionSettings(ssh_user="tunneluser")
    proxy = _proxy("proxy-1", "same-host", 12001)

    assert _extract_authorized_key(settings, proxy) == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey"


def test_key_markers_do_not_match_other_ids_with_same_prefix(tmp_path, monkeypatch):
    from .. import services

    path = tmp_path / "authorized_keys"
    path.write_text("ssh-ed25519 AAAAother reverse_proxy:proxy-10\n")
    monkeypatch.setattr(services, "_authorized_keys_path", lambda _: str(path))
    settings = ExtensionSettings(ssh_user="tunneluser")
    proxy = _proxy("proxy-1", "host", 12000)
    services._add_authorized_key(settings, proxy, "ssh-ed25519 AAAAnew")
    services._remove_authorized_key(settings, proxy)
    assert path.read_text() == "ssh-ed25519 AAAAother reverse_proxy:proxy-10\n"


def test_atomic_file_edits_preserve_concurrent_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from ..files import edit_file

    path = str(tmp_path / "authorized_keys")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: edit_file(path, lambda content: content + f"key-{i}\n"), range(24)))
    with open(path) as output:
        assert set(output.read().splitlines()) == {f"key-{i}" for i in range(24)}
