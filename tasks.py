import asyncio

from lnbits.core.models import Payment
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .services import payment_received_for_payments


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
