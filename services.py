import asyncio
import hashlib
import os
import pwd
import random
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from lnbits.core.crud import get_standalone_payment
from lnbits.core.models import Payment
from lnbits.core.services import create_invoice
from lnbits.helpers import urlsafe_short_hash
from loguru import logger
from sqlalchemy.exc import IntegrityError  # type: ignore[import-untyped]

from .crud import (
    create_extension_settings,
    create_payments,
    create_proxies,
    db,
    delete_proxies,
    get_active_or_pending_proxy_by_subdomain,
    get_all_extension_settings,
    get_extension_settings,
    get_payments_by_id,
    get_proxies,
    get_proxies_by_id,
    update_extension_settings,
)
from .files import edit_file
from .models import (
    CreatePayments,
    CreateProxies,
    CreateTunnelRequest,
    CreateTunnelResponse,
    ExtensionSettings,
    PaymentsPaymentRequest,
    Proxies,
    PublicTopupRequest,
    UserExtensionSettings,
)

MAX_PROXY_RESERVATION_ATTEMPTS = 10
_sync_lock = asyncio.Lock()
_reloaded_files: dict[str, str] = {}
_SSH_PUBLIC_KEY_RE = re.compile(
    r"(sk-ssh-ed25519@openssh\.com|ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)\s+([A-Za-z0-9+/=]+)"
)


async def get_settings(user_id: str) -> ExtensionSettings:
    settings = await get_extension_settings(user_id)
    if not settings:
        settings = await create_extension_settings(user_id, ExtensionSettings())
    return settings


async def update_settings(user_id: str, data: ExtensionSettings) -> ExtensionSettings:
    settings = await get_extension_settings(user_id)
    if not settings:
        settings = await create_extension_settings(user_id, data)
    else:
        settings = await update_extension_settings(user_id, data)

    return settings


async def _get_used_ports() -> set[int]:
    rows: list[dict] = await db.fetchall("SELECT remote_port FROM reverse_proxy.proxies")
    return {int(r["remote_port"]) for r in rows}


async def allocate_remote_port(
    remote_port_min: int,
    remote_port_max: int,
) -> int:
    if not 1 <= remote_port_min <= remote_port_max <= 65535:
        raise ValueError("Invalid remote port range.")
    used = await _get_used_ports()
    for port in range(remote_port_min, remote_port_max + 1):
        if port not in used:
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise ValueError("No remote ports available in the configured range.")


async def allocate_subdomain(
    desired: str | None = None,
    max_attempts: int = MAX_PROXY_RESERVATION_ATTEMPTS,
) -> str:
    if desired:
        existing = await get_active_or_pending_proxy_by_subdomain(desired)
        if not existing:
            return desired

    for _ in range(max_attempts):
        candidate = random_subdomain()
        existing = await get_active_or_pending_proxy_by_subdomain(candidate)
        if not existing:
            return candidate

    raise ValueError("Failed to allocate a unique subdomain.")


_SUBJECTS = [
    "otter",
    "panda",
    "lynx",
    "koala",
    "badger",
    "gecko",
    "orca",
    "fox",
    "sparrow",
    "lemur",
    "corgi",
    "yak",
    "heron",
    "marten",
    "mole",
    "shrike",
    "swift",
    "toad",
    "tern",
    "eel",
    "bunny",
    "lark",
    "finch",
    "stoat",
    "dingo",
    "quail",
    "minnow",
    "crane",
    "civet",
    "cubby",
    "sable",
    "copper",
    "drake",
    "ferret",
    "ibis",
    "jay",
    "kudu",
    "loon",
    "manx",
    "newt",
    "pika",
    "skink",
    "vole",
    "wren",
]

_MOODS = [
    "brisk",
    "brave",
    "calm",
    "bright",
    "daring",
    "eager",
    "fluent",
    "gentle",
    "happy",
    "humble",
    "keen",
    "lucky",
    "merry",
    "nifty",
    "plucky",
    "quick",
    "spry",
    "sunny",
    "tidy",
    "witty",
    "breezy",
    "cheery",
    "dapper",
    "eager",
    "fair",
    "glad",
    "jolly",
    "light",
    "mild",
    "nimble",
    "peppy",
    "rapid",
    "ready",
    "sharp",
    "spry",
    "steady",
    "swell",
    "trim",
    "zesty",
    "zippy",
]


def random_subdomain() -> str:
    """
    Human-readable subdomain with enough entropy:
    <mood><animal><4 digits>.
    """
    number = f"{random.randint(0, 9999):04d}"
    return f"{random.choice(_MOODS)}{random.choice(_SUBJECTS)}{number}"


def deterministic_subdomain(client_note: str) -> str:
    digest = hashlib.sha256(client_note.encode("utf-8")).digest()
    mood = _MOODS[digest[0] % len(_MOODS)]
    subject = _SUBJECTS[digest[1] % len(_SUBJECTS)]
    number = int.from_bytes(digest[2:4], "big") % 10000
    return f"{mood}{subject}{number:04d}"


def generate_ssh_keypair(comment: str) -> tuple[str, str]:
    """
    Generate an ed25519 keypair and return (private_key, public_key) strings.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", key_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(key_path, encoding="utf-8") as f_priv:
            private_key = f_priv.read()
        with open(f"{key_path}.pub", encoding="utf-8") as f_pub:
            public_key = f_pub.read().strip()
    return private_key, public_key


def _caddy_block(host: str, remote_port: int) -> str:
    return f"""{host} {{
  reverse_proxy 127.0.0.1:{remote_port}
}}

"""


def _sort_proxy_preference(proxy: Proxies) -> tuple[datetime, datetime, str]:
    return (
        proxy.expires_at,
        proxy.updated_at,
        proxy.id,
    )


def _select_canonical_proxies(proxies: list[Proxies], domain: str) -> list[Proxies]:
    by_host: dict[str, Proxies] = {}
    for proxy in proxies:
        host = f"{proxy.subdomain}.{domain}"
        current = by_host.get(host)
        if current is None or _sort_proxy_preference(proxy) > _sort_proxy_preference(current):
            by_host[host] = proxy
    return list(by_host.values())


async def _mark_expired_proxies(
    user_id: str,
    settings: ExtensionSettings,
    now: datetime,
) -> None:
    expired = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE (status = 'expired' OR (status = 'active' AND expires_at <= :now)) AND user_id = :user_id""",
        values={"now": now, "user_id": user_id},
        model=Proxies,
    )
    for proxy in expired:
        captured_key = proxy.ssh_public_key or _extract_authorized_key(settings, proxy)
        async with db.connect() as conn:
            expired_row = await conn.fetchone(
                f"""UPDATE reverse_proxy.proxies SET status = 'expired',
                    ssh_public_key = COALESCE(ssh_public_key, :key)
                    WHERE id = :id AND (status = 'expired' OR expires_at <= {db.timestamp_placeholder('now')})
                    RETURNING id""",
                {"id": proxy.id, "key": captured_key, "now": now},
            )
            await conn.conn.commit()
        if expired_row:
            await asyncio.to_thread(_remove_authorized_key, settings, proxy)


async def _prune_stale_proxies(
    user_id: str,
    settings: ExtensionSettings,
    prune_cutoff: datetime,
) -> None:
    stale = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE status IN ('expired', 'pending') AND expires_at <= :cutoff AND user_id = :user_id""",
        values={"cutoff": prune_cutoff, "user_id": user_id},
        model=Proxies,
    )
    for proxy in stale:
        async with db.connect() as conn:
            removed = await conn.fetchone(
                f"""DELETE FROM reverse_proxy.proxies WHERE id = :id
                    AND status IN ('expired', 'pending') AND expires_at <= {db.timestamp_placeholder('cutoff')}
                    RETURNING id""",
                {"id": proxy.id, "cutoff": prune_cutoff},
            )
            await conn.conn.commit()
        if removed:
            await asyncio.to_thread(_remove_authorized_key, settings, proxy)
            logger.info(f"reverse_proxy: pruned stale proxy {proxy.id}")


async def _load_canonical_active_proxies(
    user_id: str,
    settings: ExtensionSettings,
    now: datetime,
) -> list[Proxies]:
    active = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE status = :status AND expires_at > :now AND user_id = :user_id""",
        values={"status": "active", "now": now, "user_id": user_id},
        model=Proxies,
    )
    canonical_active = _select_canonical_proxies(active, settings.domain)
    if len(canonical_active) != len(active):
        logger.warning(
            "reverse_proxy: deduped active proxies for Caddy sync "
            f"({len(active)} rows -> {len(canonical_active)} routes)"
        )
    return canonical_active


async def _ensure_active_authorized_keys(
    settings: ExtensionSettings,
    active: list[Proxies],
) -> None:
    for proxy in active:
        if not proxy.ssh_public_key:
            proxy.ssh_public_key = _extract_authorized_key(settings, proxy)
            if proxy.ssh_public_key:
                await db.execute(
                    "UPDATE reverse_proxy.proxies SET ssh_public_key = :key WHERE id = :id AND ssh_public_key IS NULL",
                    {"id": proxy.id, "key": proxy.ssh_public_key},
                )
        if proxy.ssh_public_key:
            await asyncio.to_thread(_add_authorized_key, settings, proxy, proxy.ssh_public_key)
        else:
            raise ValueError(f"No SSH public key stored for proxy {proxy.id}")


def _remove_existing_caddy_block(caddy_file: str, host: str) -> None:
    pattern = re.compile(rf"(?ms)^\s*{re.escape(host)}\s*\{{\s*reverse_proxy\s+127\.0\.0\.1:\d+\s*\}}\s*")
    if os.path.exists(caddy_file):
        edit_file(caddy_file, lambda content: pattern.sub("", content), 0o644)


async def cleanup_expired_proxies_and_sync_caddy(user_id: str, settings: ExtensionSettings) -> None:
    # Settings may share the same file. Rebuilding from only one user's rows
    # silently deletes every other user's routes.
    async with _sync_lock:
        now = datetime.now(timezone.utc)
        owners = await get_all_extension_settings()
        owners = [owner for owner in owners if owner.caddy_tunnels_file == settings.caddy_tunnels_file]
        routes: dict[str, Proxies] = {}
        for owner in owners:
            await _mark_expired_proxies(owner.id, owner, now)
            await _prune_stale_proxies(owner.id, owner, now - timedelta(days=7))
            active = await _load_canonical_active_proxies(owner.id, owner, now)
            await _ensure_active_authorized_keys(owner, active)
            for proxy in active:
                routes[f"{proxy.subdomain}.{owner.domain}"] = proxy
        content = "".join(_caddy_block(host, proxy.remote_port) for host, proxy in sorted(routes.items()))
        path = settings.caddy_tunnels_file
        changed = await asyncio.to_thread(edit_file, path, lambda _: content, 0o644)
        if changed or _reloaded_files.get(path) != content:
            await asyncio.to_thread(
                subprocess.run,
                ["sudo", "-n", "systemctl", "reload", "caddy"],
                check=True,
                timeout=30,
            )
            _reloaded_files[path] = content


async def create_tunnel_with_invoice(
    req: CreateTunnelRequest,
    settings: UserExtensionSettings,
    extra: dict | None = None,
) -> CreateTunnelResponse:
    """
    1) Allocate subdomain + port.
    2) Persist a reservation and invoice metadata before exposing the invoice.
    3) Return all details + ssh command + payment_request.
    """
    now = datetime.now(timezone.utc)

    amount = req.days * settings.price_per_day_sats
    if amount <= 0:
        raise ValueError("Amount must be > 0.")

    # A setup request always reserves a new key/port pair. Renewal uses the
    # top-up endpoint; client_note is metadata, never authority to rotate a key.
    proxy_id = urlsafe_short_hash()
    payments_id = urlsafe_short_hash()
    try:
        private_key, public_key = generate_ssh_keypair(f"reverse_proxy {proxy_id}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to generate ssh keypair: {exc}")
        raise

    created_proxy = False
    desired_subdomain = deterministic_subdomain(req.client_note) if req.client_note else None
    last_error: Exception | None = None
    for _ in range(MAX_PROXY_RESERVATION_ATTEMPTS):
        subdomain = await allocate_subdomain(desired_subdomain)
        remote_port = await allocate_remote_port(settings.remote_port_min, settings.remote_port_max)
        try:
            await create_proxies(
                user_id=settings.id,
                data=CreateProxies(
                    subdomain=subdomain,
                    remote_port=remote_port,
                    ssh_user=settings.ssh_user,
                    ssh_public_key=public_key,
                    expires_at=now,
                    status="pending",
                    public_id=req.public_id,
                    client_note=req.client_note or "",
                ),
                proxies_id=proxy_id,
            )
            created_proxy = True
            break
        except IntegrityError as exc:
            last_error = exc
            logger.warning(
                "reverse_proxy: proxy reservation collision for "
                f"subdomain={subdomain} remote_port={remote_port}, retrying"
            )
            desired_subdomain = None
    else:
        raise ValueError("Failed to reserve a unique proxy.") from last_error

    expires_at = now  # will be extended on first payment

    memo = f"Reverse proxy {subdomain}.{settings.domain} for {req.days} day(s)"
    invoice_extra = {**(extra or {}), "tag": "reverse_proxy"}
    invoice_extra["days"] = req.days
    invoice_extra["proxy_setup"] = {
        "proxy_id": proxy_id,
        "payments_id": payments_id,
        "subdomain": subdomain,
        "remote_port": remote_port,
        "ssh_user": settings.ssh_user,
        "ssh_host": settings.ssh_host,
        "domain": settings.domain,
        "public_id": req.public_id,
        "client_note": req.client_note or "",
        "pubkey": public_key,
    }
    # Persist the reservation before exposing an invoice to the payment listener.
    invoice_record = await create_payments(
        proxies_id=proxy_id,
        data=CreatePayments(tunnel_id=proxy_id, invoice_id="", amount=amount, paid=False),
        payments_id=payments_id,
    )
    try:
        payment: Payment = await create_invoice(
            wallet_id=settings.wallet,
            amount=amount,
            extra=invoice_extra,
            memo=memo,
        )
    except Exception:
        if created_proxy:
            await delete_proxies(settings.id, proxy_id)
        raise
    # Update only the hash: a fast payment may already have settled this record.
    await db.execute(
        "UPDATE reverse_proxy.payments SET invoice_id = :hash WHERE id = :id",
        {"hash": payment.payment_hash, "id": invoice_record.id},
    )

    ssh_command = (
        f"ssh -i ./reverse-proxy-key -N -R {remote_port}:localhost:5000 " f"{settings.ssh_user}@{settings.ssh_host}"
    )
    public_url = f"https://{subdomain}.{settings.domain}"

    return CreateTunnelResponse(
        tunnel_id=proxy_id,
        subdomain=subdomain,
        remote_port=remote_port,
        ssh_user=settings.ssh_user,
        ssh_host=settings.ssh_host,
        ssh_private_key=private_key,
        ssh_command=ssh_command,
        public_url=public_url,
        expires_at=expires_at,
        payment_hash=payment.payment_hash,
        payment_request=payment.bolt11,
        client_note=req.client_note or "",
    )


async def payment_received_for_payments(payment: Payment) -> bool:
    """Credit each settled invoice once, atomically with its expiry extension.

    Filesystem provisioning happens after commit and is deliberately retryable.
    Never rotate a reserved key when an older setup invoice is replayed.
    """
    if not payment.success or payment.amount <= 0 or payment.extra.get("tag") != "reverse_proxy":
        return False
    setup = payment.extra.get("proxy_setup") or {}
    payments_id = setup.get("payments_id") or payment.extra.get("payments_id")
    record = await get_payments_by_id(payments_id) if payments_id else None
    if not record:
        return False
    proxy = await get_proxies_by_id(record.proxies_id)
    if not proxy or record.tunnel_id != proxy.id:
        return False
    settings = await get_settings(proxy.user_id)
    if payment.wallet_id != settings.wallet or record.invoice_id not in (
        "",
        payment.payment_hash,
    ):
        return False
    valid_amounts = {record.amount * 1000, record.amount} if record.paid else {record.amount * 1000}
    if payment.amount not in valid_amounts:
        return False
    days = payment.extra.get("days")
    if days is None:
        days = (payment.amount / 1000) / settings.price_per_day_sats
    if not isinstance(days, (int, float)) or days <= 0:
        return False

    async with db.connect() as conn:
        # Use fetchone for SQL writes: LNbits Connection.update/execute commit
        # each statement, which would split the credit and paid flag.
        claimed = await conn.fetchone(
            """UPDATE reverse_proxy.payments SET paid = true, invoice_id = :hash
               WHERE id = :id AND paid = false RETURNING id""",
            {"id": record.id, "hash": payment.payment_hash},
        )
        if claimed:
            # Acquire the proxy row lock before reading expiry, also serializing
            # distinct invoices for the same proxy on PostgreSQL.
            proxy = await conn.fetchone(
                """UPDATE reverse_proxy.proxies SET updated_at = updated_at
                   WHERE id = :id RETURNING *""",
                {"id": proxy.id},
                Proxies,
            )
            if not proxy:
                await conn.conn.rollback()
                return False
            now = datetime.now(timezone.utc)
            expiry = max(proxy.expires_at, now) + timedelta(days=days)
            await conn.fetchone(
                f"""UPDATE reverse_proxy.proxies SET status = 'active',
                    expires_at = {db.timestamp_placeholder('expiry')},
                    updated_at = {db.timestamp_placeholder('now')}
                    WHERE id = :id RETURNING id""",
                {"id": proxy.id, "expiry": expiry, "now": now},
            )
        await conn.conn.commit()

    await cleanup_expired_proxies_and_sync_caddy(proxy.user_id, settings)
    return True


async def tunnel_payment_status(tunnel_id: str, payment_hash: str) -> dict | None:
    """The invoice hash is a capability, scoped to one tunnel and wallet."""
    proxy = await get_proxies_by_id(tunnel_id)
    if not proxy:
        return None
    settings = await get_settings(proxy.user_id)
    payment = await get_standalone_payment(payment_hash, incoming=True, wallet_id=settings.wallet)
    if not payment or payment.extra.get("tag") != "reverse_proxy":
        return None
    setup = payment.extra.get("proxy_setup") or {}
    record_id = setup.get("payments_id") or payment.extra.get("payments_id")
    record = await get_payments_by_id(record_id) if record_id else None
    if not record or record.proxies_id != tunnel_id or record.invoice_id not in ("", payment_hash):
        return None
    ready = False
    if payment.success:
        ready = await payment_received_for_payments(payment)
        proxy = await get_proxies_by_id(tunnel_id)
        if not proxy:
            return None
    return {
        "tunnel_id": tunnel_id,
        "paid": payment.success,
        "invoice_expired": bool(payment.expiry and payment.expiry <= datetime.now(timezone.utc)),
        "ready": ready and proxy.status == "active" and proxy.expires_at > datetime.now(timezone.utc),
        "expires_at": proxy.expires_at,
        "status": proxy.status,
    }


def _authorized_keys_path(settings: ExtensionSettings) -> str:
    return os.path.join(pwd.getpwnam(settings.ssh_user).pw_dir, ".ssh", "authorized_keys")


def _normalize_public_key(pubkey: str | None) -> str | None:
    if not pubkey:
        return None
    match = _SSH_PUBLIC_KEY_RE.search(pubkey.strip())
    if not match:
        return None
    return f"{match.group(1)} {match.group(2)}"


def _extract_authorized_key(settings: ExtensionSettings, proxy: Proxies) -> str | None:
    """
    Try to read the stored public key for this proxy from authorized_keys.
    """
    account = settings.model_copy(update={"ssh_user": proxy.ssh_user})
    ak_path = _authorized_keys_path(account)
    marker = f"reverse_proxy:{proxy.id}"
    if not os.path.exists(ak_path):
        return None

    try:
        with open(ak_path, encoding="utf-8") as f:
            for line in f:
                if not _has_marker(line, marker):
                    continue
                normalized = _normalize_public_key(line)
                if normalized:
                    return normalized
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to read authorized_key for {proxy.id}: {exc}")
    return None


def _has_marker(line: str, marker: str) -> bool:
    return re.search(rf"(?<![\w:-]){re.escape(marker)}(?![\w:-])", line) is not None


def _add_authorized_key(settings: ExtensionSettings, proxy: Proxies, pubkey: str) -> None:
    normalized = _normalize_public_key(pubkey)
    if not normalized:
        raise ValueError(f"Invalid SSH public key for proxy {proxy.id}")
    # Keep the account assigned to the reservation, even if defaults change.
    account = settings.model_copy(update={"ssh_user": proxy.ssh_user})
    path = _authorized_keys_path(account)
    marker = f"reverse_proxy:{proxy.id}"
    options = (
        'restrict,port-forwarding,permitopen="no-local-forward.invalid:1",'
        f'permitlisten="127.0.0.1:{proxy.remote_port}",'
        f'permitlisten="localhost:{proxy.remote_port}",command="/bin/false"'
    )
    line = f"{options} {normalized} {marker}"

    def replace(content: str) -> str:
        lines = [entry for entry in content.splitlines() if not _has_marker(entry, marker)]
        return "\n".join([*lines, line]) + "\n"

    edit_file(path, replace)


def _remove_authorized_key(settings: ExtensionSettings, proxy: Proxies) -> None:
    account = settings.model_copy(update={"ssh_user": proxy.ssh_user})
    path = _authorized_keys_path(account)
    marker = f"reverse_proxy:{proxy.id}"
    if os.path.exists(path):
        edit_file(
            path,
            lambda content: "".join(
                line for line in content.splitlines(keepends=True) if not _has_marker(line, marker)
            ),
        )


async def payment_request_for_payments(
    proxies_id: str,
    data: CreatePayments | PublicTopupRequest,
) -> PaymentsPaymentRequest:
    proxies = await get_proxies_by_id(proxies_id)
    if not proxies:
        raise ValueError("Invalid proxies ID.")

    settings = await get_settings(proxies.user_id)

    # derive amount from days when provided
    amount = getattr(data, "amount", 0)
    if hasattr(data, "days"):
        amount = getattr(data, "days", 0) * settings.price_per_day_sats
    if not amount or amount <= 0:
        raise ValueError("The amount must be greater than zero to create a payment request.")

    payments = await create_payments(
        proxies_id,
        CreatePayments(
            tunnel_id=proxies_id,
            invoice_id="",
            amount=amount,
            paid=False,
        ),
    )

    proxies_name = f"{proxies.subdomain}.{settings.domain}"
    extra = {
        "tag": "reverse_proxy",
        "payments_id": payments.id,
        "days": amount / settings.price_per_day_sats,
    }
    payment: Payment = await create_invoice(
        wallet_id=settings.wallet,
        amount=amount,
        extra=extra,
        memo=f"Payment for {proxies_name}. Payments ID: {payments.id}",
    )
    await db.execute(
        "UPDATE reverse_proxy.payments SET invoice_id = :hash WHERE id = :id",
        {"hash": payment.payment_hash, "id": payments.id},
    )
    payments_resp = PaymentsPaymentRequest(
        payments_id=payments.id,
        payment_hash=payment.payment_hash,
        payment_request=payment.bolt11,
    )
    return payments_resp


async def delete_proxy_and_key(user_id: str, proxy_id: str, settings: ExtensionSettings) -> None:
    proxy = await get_proxies(user_id, proxy_id)
    if proxy:
        _remove_authorized_key(settings, proxy)
    await delete_proxies(user_id, proxy_id)
    await cleanup_expired_proxies_and_sync_caddy(user_id, settings)
