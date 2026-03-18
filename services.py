import hashlib
import os
import random
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from lnbits.core.models import Payment
from lnbits.core.services import create_invoice
from lnbits.helpers import urlsafe_short_hash
from loguru import logger
from sqlalchemy.exc import IntegrityError

from .crud import (
    create_extension_settings,
    create_payments,
    create_proxies,
    db,
    delete_proxies,
    get_active_or_pending_proxy_by_subdomain,
    get_extension_settings,
    get_extension_settings_public,
    get_payments_by_id,
    get_proxies,
    get_proxies_by_id,
    get_proxies_by_public_id_and_client_note,
    update_extension_settings,
    update_payments,
    update_proxies,
)
from .models import (
    CreatePayments,
    CreateProxies,
    CreateTunnelRequest,
    CreateTunnelResponse,
    ExtensionSettings,
    PaymentsPaymentRequest,
    Proxies,
    PublicTopupRequest,
)

MAX_PROXY_RESERVATION_ATTEMPTS = 10


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
    rows = await db.fetchall("SELECT remote_port FROM reverse_proxy.proxies")
    return {int(r["remote_port"]) for r in rows}


async def allocate_remote_port(
    remote_port_min: int,
    remote_port_max: int,
) -> int:
    used = await _get_used_ports()
    for port in range(remote_port_min, remote_port_max):
        if port not in used:
            return port
    # fallback: random in range (very unlikely collision)
    while True:
        port = random.randint(remote_port_min, remote_port_max)
        if port not in used:
            return port


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
            WHERE status = :status AND expires_at <= :now AND user_id = :user_id""",
        values={"status": "active", "now": now, "user_id": user_id},
        model=Proxies,
    )
    for proxy in expired:
        if not proxy.ssh_public_key:
            captured_key = _extract_authorized_key(settings, proxy)
            if captured_key:
                proxy.ssh_public_key = captured_key
        proxy.status = "expired"
        await update_proxies(proxy)
        _remove_authorized_key(settings, proxy)
        logger.info(
            f"reverse_proxy: marked proxy {proxy.id} as expired (was active, "
            f"expires_at={proxy.expires_at})"
        )


async def _prune_stale_proxies(
    user_id: str,
    settings: ExtensionSettings,
    prune_cutoff: datetime,
) -> None:
    stale = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE status = :status AND expires_at <= :cutoff AND user_id = :user_id""",
        values={"status": "expired", "cutoff": prune_cutoff, "user_id": user_id},
        model=Proxies,
    )
    for proxy in stale:
        _remove_authorized_key(settings, proxy)
        await delete_proxies(user_id, proxy.id)
        logger.info(
            f"reverse_proxy: pruned expired proxy {proxy.id} "
            f"(expired at {proxy.expires_at}, older than 7 days)"
        )


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
                await update_proxies(proxy)
        if proxy.ssh_public_key:
            _add_authorized_key(settings, proxy, proxy.ssh_public_key)


def ensure_caddy_route(proxy: Proxies, settings: ExtensionSettings) -> None:
    """
    Append (or re-append) a route for subdomain -> remote_port to tunnels.caddy
    and reload Caddy. This is idempotent-ish because we also have a full
    sync function that rewrites the file.
    """
    host = f"{proxy.subdomain}.{settings.domain}"
    _remove_existing_caddy_block(settings.caddy_tunnels_file, host)

    try:
        with open(settings.caddy_tunnels_file, "a", encoding="utf-8") as f:
            f.write(_caddy_block(host, proxy.remote_port))

        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
        )
        logger.info(f"reverse_proxy: appended Caddy route for {host}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to append Caddy route for {host}: {exc}")
        # payment is still valid; tunnel will get picked up on next full sync


def _remove_existing_caddy_block(caddy_file: str, host: str) -> None:
    """
    Remove any existing block for `host` to avoid duplicate site definitions.
    """
    if not os.path.exists(caddy_file):
        return

    try:
        with open(caddy_file, encoding="utf-8") as f:
            content = f.read()

        patterns = [
            re.compile(
                rf"(?ms)^\s*{re.escape(host)}\s*\{{\s*reverse_proxy\s+127\.0\.0\.1:\d+\s*\}}\s*"
            ),
            re.compile(
                rf"(?m)^\s*{re.escape(host)}\s*\{{\s*$.*?^\}}\s*$\n?",
                re.DOTALL,
            ),
        ]
        updated = content
        for pattern in patterns:
            updated = pattern.sub("", updated)

        if updated != content:
            with open(caddy_file, "w", encoding="utf-8") as f:
                f.write(updated)
            logger.info(f"reverse_proxy: removed duplicate Caddy block for {host}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to dedupe Caddy block for {host}: {exc}")


async def cleanup_expired_proxies_and_sync_caddy(
    user_id: str,
    settings: ExtensionSettings,
) -> None:
    """
    1) Mark any active proxies whose expires_at is in the past as 'expired'.
    2) Rebuild the Caddy tunnels file from currently active, non-expired proxies.
    3) Reload Caddy.

    This is safe to run opportunistically, e.g. every time the extension
    index page is opened.
    """
    now = datetime.now(timezone.utc)
    prune_cutoff = now - timedelta(days=7)

    await _mark_expired_proxies(user_id, settings, now)
    await _prune_stale_proxies(user_id, settings, prune_cutoff)
    canonical_active = await _load_canonical_active_proxies(user_id, settings, now)
    await _ensure_active_authorized_keys(settings, canonical_active)

    # 3) Rewrite Caddy tunnels file
    try:
        os.makedirs(os.path.dirname(settings.caddy_tunnels_file), exist_ok=True)
        with open(settings.caddy_tunnels_file, "w", encoding="utf-8") as f:
            for proxy in canonical_active:
                host = f"{proxy.subdomain}.{settings.domain}"
                f.write(_caddy_block(host, proxy.remote_port))

        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
        )
        logger.info(
            f"reverse_proxy: synced Caddy tunnels file with "
            f"{len(canonical_active)} active proxies"
        )
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to sync Caddy routes: {exc}")


async def create_tunnel_with_invoice(
    req: CreateTunnelRequest,
    settings: ExtensionSettings,
    extra: dict | None = None,
) -> CreateTunnelResponse:
    """
    1) Allocate subdomain + port.
    2) Create LNbits invoice with extra tag and proxy details (no DB writes).
    3) Return all details + ssh command + payment_request.
    """
    now = datetime.now(timezone.utc)

    amount = req.days * settings.price_per_day_sats
    if amount <= 0:
        raise ValueError("Amount must be > 0.")

    existing_proxy: Proxies | None = None
    if req.client_note:
        existing = await get_proxies_by_public_id_and_client_note(
            req.public_id, req.client_note
        )
        if existing:
            existing_proxy = max(existing, key=_sort_proxy_preference)

    proxy_id = existing_proxy.id if existing_proxy else urlsafe_short_hash()
    payments_id = urlsafe_short_hash()
    try:
        private_key, public_key = generate_ssh_keypair(f"reverse_proxy {proxy_id}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to generate ssh keypair: {exc}")
        raise

    created_proxy = False
    if existing_proxy:
        subdomain = existing_proxy.subdomain
        remote_port = existing_proxy.remote_port
    else:
        desired_subdomain = (
            deterministic_subdomain(req.client_note) if req.client_note else None
        )
        last_error: Exception | None = None
        for _ in range(MAX_PROXY_RESERVATION_ATTEMPTS):
            subdomain = await allocate_subdomain(desired_subdomain)
            remote_port = await allocate_remote_port(
                settings.remote_port_min, settings.remote_port_max
            )
            proxy_id = urlsafe_short_hash()
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

    await create_payments(
        proxies_id=proxy_id,
        data=CreatePayments(
            tunnel_id=proxy_id,
            invoice_id=payment.payment_hash,
            amount=amount,
            paid=False,
        ),
        payments_id=payments_id,
    )

    ssh_command = (
        f"ssh -i ./reverse-proxy-key -N -R {remote_port}:localhost:5000 "
        f"{settings.ssh_user}@{settings.ssh_host}"
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
    proxy_setup = payment.extra.get("proxy_setup") or {}
    if proxy_setup:
        return await _process_payment_with_setup(payment, proxy_setup)

    payments_id = payment.extra.get("payments_id")
    if not payments_id:
        logger.warning("Payment does not have a payments_id in extra.")
        return False

    payments = await get_payments_by_id(payments_id)
    if not payments:
        logger.warning(f"No payments found for ID: {payments_id}")
        return False

    if getattr(payments, "paid", False):
        logger.info(f"reverse_proxy: payment {payments_id} already processed, skipping")
        return True

    proxy = await get_proxies_by_id(getattr(payments, "tunnel_id", ""))
    if not proxy:
        logger.warning(
            f"No proxy found for ID: {getattr(payments, 'tunnel_id', '')}"
        )
        return False

    settings = await get_settings(proxy.user_id)
    price_per_day = settings.price_per_day_sats or 0
    if price_per_day <= 0:
        logger.warning("reverse_proxy: invalid price_per_day_sats in settings")
        return False

    if not proxy.ssh_public_key:
        proxy.ssh_public_key = _extract_authorized_key(settings, proxy)

    days_to_extend = (payment.amount / 1000) / price_per_day

    now = datetime.now(timezone.utc)
    current_expiry = proxy.expires_at or now
    base = current_expiry if current_expiry > now else now

    proxy.expires_at = base + timedelta(days=days_to_extend)
    proxy.status = "active"
    await update_proxies(proxy)

    if proxy.ssh_public_key:
        _add_authorized_key(settings, proxy, proxy.ssh_public_key)

    payments.paid = True
    await update_payments(payments)

    # Append route now; full sync will clean things later
    ensure_caddy_route(proxy, settings)

    logger.info(
        f"reverse_proxy: payment {payments_id} processed, proxy {proxy.id} "
        f"active until {proxy.expires_at.isoformat()}"
    )
    return True


async def _process_payment_with_setup(payment: Payment, setup: dict) -> bool:
    proxy_id = setup.get("proxy_id")
    payments_id = setup.get("payments_id")
    public_id = setup.get("public_id")
    pubkey = setup.get("pubkey")
    if not all([proxy_id, payments_id, public_id]):
        logger.warning("reverse_proxy: payment missing required proxy_setup fields")
        return False

    settings = await get_extension_settings_public(public_id)
    if not settings or not getattr(settings, "id", None):
        logger.warning("reverse_proxy: missing settings for proxy_setup public_id")
        return False
    owner_id = settings.id
    price_per_day = settings.price_per_day_sats or 0
    if price_per_day <= 0:
        logger.warning("reverse_proxy: invalid price_per_day_sats in settings")
        return False

    proxy = await get_proxies_by_id(proxy_id)
    now = datetime.now(timezone.utc)
    expires_at = now
    remote_port = int(setup.get("remote_port", 0))
    if remote_port <= 0:
        remote_port = await allocate_remote_port(
            settings.remote_port_min, settings.remote_port_max
        )

    if not proxy:
        proxy = await create_proxies(
            user_id=owner_id,
            data=CreateProxies(
                subdomain=setup.get("subdomain", random_subdomain()),
                remote_port=remote_port,
                ssh_user=setup.get("ssh_user", settings.ssh_user),
                ssh_public_key=pubkey,
                expires_at=expires_at,
                status="pending",
                public_id=public_id,
                client_note=setup.get("client_note", ""),
            ),
            proxies_id=proxy_id,
        )
    if pubkey:
        proxy.ssh_public_key = pubkey
    if getattr(proxy, "status", "") != "active":
        proxy.status = "active"

    days_to_extend = (payment.amount / 1000) / price_per_day
    current_expiry = proxy.expires_at or now
    base = current_expiry if current_expiry > now else now
    proxy.expires_at = base + timedelta(days=days_to_extend)
    await update_proxies(proxy)

    payments = await get_payments_by_id(payments_id)
    if not payments:
        payments = await create_payments(
            proxies_id=proxy.id,
            data=CreatePayments(
                tunnel_id=proxy.id,
                invoice_id=payment.payment_hash,
                amount=payment.amount,
                paid=True,
            ),
            payments_id=payments_id,
        )
    else:
        payments.invoice_id = payment.payment_hash
        payments.amount = payment.amount
        payments.paid = True
        await update_payments(payments)

    if pubkey:
        _add_authorized_key(settings, proxy, pubkey)

    ensure_caddy_route(proxy, settings)
    logger.info(
        f"reverse_proxy: payment {payments_id} processed (proxy_setup), proxy {proxy.id} "
        f"active until {proxy.expires_at.isoformat()}"
    )
    return True


def _authorized_keys_path(settings: ExtensionSettings) -> str:
    return os.path.join("/home", settings.ssh_user, ".ssh", "authorized_keys")


def _extract_authorized_key(settings: ExtensionSettings, proxy: Proxies) -> str | None:
    """
    Try to read the stored public key for this proxy from authorized_keys.
    """
    ak_path = _authorized_keys_path(settings)
    marker = f"reverse_proxy:{proxy.id}"
    if not os.path.exists(ak_path):
        return None

    try:
        with open(ak_path, encoding="utf-8") as f:
            for line in f:
                if marker not in line:
                    continue
                parts = line.strip().split()
                if len(parts) < 3 or parts[-1] != marker:
                    continue
                return " ".join(parts[1:-1])
    except Exception as exc:
        logger.error(
            f"reverse_proxy: failed to read authorized_key for {proxy.id}: {exc}"
        )
    return None


def _add_authorized_key(settings: ExtensionSettings, proxy: Proxies, pubkey: str) -> None:
    """
    Append a restricted SSH key for this proxy.
    """
    if not pubkey:
        return
    ak_path = _authorized_keys_path(settings)
    os.makedirs(os.path.dirname(ak_path), exist_ok=True)
    marker = f"reverse_proxy:{proxy.id}"
    options = (
        f'no-pty,no-user-rc,no-X11-forwarding,no-agent-forwarding,'
        f'permitopen="127.0.0.1:{proxy.remote_port}",'
        f'command="echo {marker}"'
    )
    line = f"{options} {pubkey} {marker}"
    try:
        existing = []
        if os.path.exists(ak_path):
            with open(ak_path, encoding="utf-8") as f:
                existing = [ln.rstrip("\n") for ln in f.readlines()]
        new_lines = [ln for ln in existing if marker not in ln]
        new_lines.append(line)
        with open(ak_path, "w", encoding="utf-8") as f:
            if new_lines:
                f.write("\n".join(new_lines) + "\n")
        try:
            os.chmod(ak_path, 0o600)
        except Exception:
            pass
        logger.info(f"reverse_proxy: ensured authorized_key for proxy {proxy.id}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to add authorized_key for {proxy.id}: {exc}")


def _remove_authorized_key(settings: ExtensionSettings, proxy: Proxies) -> None:
    ak_path = _authorized_keys_path(settings)
    marker = f"reverse_proxy:{proxy.id}"
    if not os.path.exists(ak_path):
        return
    try:
        with open(ak_path, encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = [ln for ln in lines if marker not in ln]
        if len(new_lines) == len(lines):
            return
        with open(ak_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info(f"reverse_proxy: removed authorized_key for proxy {proxy.id}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to remove authorized_key for {proxy.id}: {exc}")


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

    proxies_name = getattr(proxies, "", "Unknown")
    extra = {"tag": "reverse_proxy", "payments_id": payments.id}
    payment: Payment = await create_invoice(
        wallet_id=settings.wallet,
        amount=amount,
        extra=extra,
        memo=f"Payment for {proxies_name}. Payments ID: {payments.id}",
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
