import os
import random
import string
import subprocess
from datetime import datetime, timezone, timedelta

from loguru import logger

from lnbits.core.models import Payment
from lnbits.core.services import create_invoice

from .crud import (
    create_payments,
    create_proxies,
    get_payments_by_id,
    get_proxies_by_id,
    update_payments,
    update_proxies,
    db,
)
from .models import (
    PaymentsPaymentRequest,
    CreatePayments,
    CreateProxies,
    Proxies,
    CreateTunnelRequest,
    CreateTunnelResponse,
)

# Config – override via env if you like
DOMAIN = os.getenv("REVERSE_PROXY_DOMAIN", "satsy.co")
SSH_USER = os.getenv("REVERSE_PROXY_SSH_USER", "tunneluser")
SSH_HOST = os.getenv("REVERSE_PROXY_SSH_HOST", DOMAIN)
CADDY_TUNNELS_FILE = os.getenv("REVERSE_PROXY_CADDY_FILE", "/etc/caddy/tunnels.caddy")
REMOTE_PORT_MIN = int(os.getenv("REVERSE_PROXY_PORT_MIN", "12000"))
REMOTE_PORT_MAX = int(os.getenv("REVERSE_PROXY_PORT_MAX", "13000"))
PRICE_PER_DAY_SATS = int(os.getenv("REVERSE_PROXY_PRICE_PER_DAY", "100"))


async def _get_used_ports() -> set[int]:
    rows = await db.fetchall(
        "SELECT remote_port FROM reverse_proxy.proxies",
        model=Proxies,
    )
    return {int(r.remote_port) for r in rows}


async def allocate_remote_port() -> int:
    used = await _get_used_ports()
    for port in range(REMOTE_PORT_MIN, REMOTE_PORT_MAX):
        if port not in used:
            return port
    # fallback: random in range (very unlikely collision)
    while True:
        port = random.randint(REMOTE_PORT_MIN, REMOTE_PORT_MAX)
        if port not in used:
            return port


def random_subdomain(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def ensure_caddy_route(proxy: Proxies) -> None:
    """
    Append (or re-append) a route for subdomain -> remote_port to tunnels.caddy
    and reload Caddy. This is idempotent-ish because we also have a full
    sync function that rewrites the file.
    """
    host = f"{proxy.subdomain}.{DOMAIN}"
    block = f"""{host} {{
  reverse_proxy 127.0.0.1:{proxy.remote_port}
}}

"""

    try:
        with open(CADDY_TUNNELS_FILE, "a") as f:
            f.write(block)

        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
        )
        logger.info(f"reverse_proxy: appended Caddy route for {host}")
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to append Caddy route for {host}: {exc}")
        # payment is still valid; tunnel will get picked up on next full sync


async def cleanup_expired_proxies_and_sync_caddy() -> None:
    """
    1) Mark any active proxies whose expires_at is in the past as 'expired'.
    2) Rebuild the Caddy tunnels file from currently active, non-expired proxies.
    3) Reload Caddy.

    This is safe to run opportunistically, e.g. every time the extension
    index page is opened.
    """
    now = datetime.now(timezone.utc)

    # 1) Mark expired
    expired = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE status = :status AND expires_at <= :now""",
        values={"status": "active", "now": now},
        model=Proxies,
    )
    for proxy in expired:
        proxy.status = "expired"
        await update_proxies(proxy)
        logger.info(
            f"reverse_proxy: marked proxy {proxy.id} as expired (was active, "
            f"expires_at={proxy.expires_at})"
        )

    # 2) Load still-active proxies
    active = await db.fetchall(
        """SELECT * FROM reverse_proxy.proxies
            WHERE status = :status AND expires_at > :now""",
        values={"status": "active", "now": now},
        model=Proxies,
    )

    # 3) Rewrite Caddy tunnels file
    try:
        os.makedirs(os.path.dirname(CADDY_TUNNELS_FILE), exist_ok=True)
        with open(CADDY_TUNNELS_FILE, "w") as f:
            for proxy in active:
                host = f"{proxy.subdomain}.{DOMAIN}"
                f.write(
                    f"""{host} {{
  reverse_proxy 127.0.0.1:{proxy.remote_port}
}}

"""
                )

        subprocess.run(
            ["sudo", "systemctl", "reload", "caddy"],
            check=True,
        )
        logger.info(
            f"reverse_proxy: synced Caddy tunnels file with "
            f"{len(active)} active proxies"
        )
    except Exception as exc:
        logger.error(f"reverse_proxy: failed to sync Caddy routes: {exc}")


async def create_tunnel_with_invoice(
    user_id: str,
    wallet_id: str,
    req: CreateTunnelRequest,
) -> CreateTunnelResponse:
    """
    1) Allocate subdomain + port.
    2) Create proxies row (status=pending, expires_at = now).
    3) Create payments row (amount = days * PRICE_PER_DAY_SATS).
    4) Create LNbits invoice with extra tag + payments_id.
    5) Return all details + ssh command + payment_request.
    """
    now = datetime.now(timezone.utc)
    expires_at = now  # will be extended on first payment

    remote_port = await allocate_remote_port()
    subdomain = random_subdomain()

    proxy = await create_proxies(
        user_id=user_id,
        data=CreateProxies(
            customer_label=req.customer_label,
            subdomain=subdomain,
            remote_port=remote_port,
            ssh_user=SSH_USER,
            expires_at=expires_at,
            status="pending",
            wallet=wallet_id,
        ),
    )

    amount = req.days * PRICE_PER_DAY_SATS
    if amount <= 0:
        raise ValueError("Amount must be > 0.")

    payments = await create_payments(
        proxies_id=proxy.id,
        data=CreatePayments(
            tunnel_id=proxy.id,
            invoice_id="",
            amount=amount,
            paid=False,
        ),
    )

    memo = f"Reverse proxy {proxy.subdomain}.{DOMAIN} for {req.days} day(s)"

    payment: Payment = await create_invoice(
        wallet_id=wallet_id,
        amount=amount,
        extra={"tag": "reverse_proxy", "payments_id": payments.id},
        memo=memo,
    )

    payments.invoice_id = payment.payment_hash
    await update_payments(payments)

    ssh_command = (
        f"ssh -N -R {proxy.remote_port}:localhost:5000 "
        f"{SSH_USER}@{SSH_HOST}"
    )
    public_url = f"https://{proxy.subdomain}.{DOMAIN}"

    return CreateTunnelResponse(
        tunnel_id=proxy.id,
        subdomain=proxy.subdomain,
        remote_port=proxy.remote_port,
        ssh_user=SSH_USER,
        ssh_host=SSH_HOST,
        ssh_command=ssh_command,
        public_url=public_url,
        expires_at=proxy.expires_at,
        payment_hash=payment.payment_hash,
        payment_request=payment.bolt11,
    )


async def payment_received_for_payments(payment: Payment) -> bool:
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

    # amount is sats; PRICE_PER_DAY_SATS sats = 1 day
    days_to_extend = payment.amount / PRICE_PER_DAY_SATS

    now = datetime.now(timezone.utc)
    current_expiry = proxy.expires_at or now
    base = current_expiry if current_expiry > now else now

    proxy.expires_at = base + timedelta(days=days_to_extend)
    proxy.status = "active"
    await update_proxies(proxy)

    payments.paid = True
    await update_payments(payments)

    # Append route now; full sync will clean things later
    ensure_caddy_route(proxy)

    logger.info(
        f"reverse_proxy: payment {payments_id} processed, proxy {proxy.id} "
        f"active until {proxy.expires_at.isoformat()}"
    )
    return True


async def payment_request_for_payments(
    proxies_id: str,
    data: CreatePayments,
) -> PaymentsPaymentRequest:
    proxies = await get_proxies_by_id(proxies_id)
    if not proxies:
        raise ValueError("Invalid proxies ID.")

    payments = await create_payments(proxies_id, data)

    amount = getattr(payments, "amount", 0)
    if not amount or amount <= 0:
        raise ValueError("The amount must be greater than zero to create a payment request.")

    proxies_name = getattr(proxies, "", "Unknown")
    payment: Payment = await create_invoice(
        wallet_id=getattr(proxies, "wallet", "no_wallet_id"),
        amount=amount,
        currency=getattr(proxies, "", "sats"),
        extra={"tag": "reverse_proxy", "payments_id": payments.id},
        memo=f"Payment for {proxies_name}. Payments ID: {payments.id}",
    )
    payments_resp = PaymentsPaymentRequest(
        payments_id=payments.id,
        payment_hash=payment.payment_hash,
        payment_request=payment.bolt11,
    )
    return payments_resp
