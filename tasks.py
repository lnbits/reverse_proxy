import asyncio

from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener, run_interval
from loguru import logger

from .crud import get_all_extension_settings
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
    settings_list = await get_all_extension_settings()
    for settings in settings_list:
        try:
            await cleanup_expired_proxies_and_sync_caddy(settings.id, settings)
        except Exception as exc:
            logger.error(
                f"reverse_proxy: cleanup failed for user {settings.id}: {exc}"
            )


# Runs every hour while the app is running
cleanup_tunnels_hourly = run_interval(3600, _cleanup_all_tunnels_once)
