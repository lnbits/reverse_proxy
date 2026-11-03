import asyncio

from lnbits.core.crud import get_standalone_payment
from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import db, get_all_extension_settings
from .services import (
    cleanup_expired_proxies_and_sync_caddy,
    payment_received_for_payments,
)


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_reverse_proxy")
    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


# Do something when an invoice related to this extension is paid
async def on_invoice_paid(payment: Payment) -> None:
    if payment.extra.get("tag") != "reverse_proxy":
        return

    logger.info(f"Invoice paid for reverse_proxy: {payment.payment_hash}")

    try:
        await payment_received_for_payments(payment)
    except Exception as e:
        logger.error(f"Error processing payment for reverse_proxy: {e}")


async def _cleanup_all_tunnels_once():
    # Recover events missed during restart or failed provisioning.
    rows = await db.fetchall("SELECT invoice_id FROM reverse_proxy.payments WHERE paid = false AND invoice_id != ''")
    for row in rows:
        try:
            payment = await get_standalone_payment(row["invoice_id"], incoming=True)
            if payment and payment.success:
                await payment_received_for_payments(payment)
        except Exception as exc:
            logger.error(f"reverse_proxy: payment recovery failed: {exc}")
    settings_list = await get_all_extension_settings()
    for settings in settings_list:
        try:
            await cleanup_expired_proxies_and_sync_caddy(settings.id, settings)
        except Exception as exc:
            logger.error(f"reverse_proxy: cleanup failed for user {settings.id}: {exc}")


async def cleanup_tunnels_hourly():
    # Run immediately on startup and retry provisioning failures within a minute.
    while True:
        try:
            await _cleanup_all_tunnels_once()
        except Exception as exc:
            logger.error(f"reverse_proxy: reconciliation failed: {exc}")
        await asyncio.sleep(60)
