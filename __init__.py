import asyncio

from fastapi import APIRouter
from lnbits.tasks import create_permanent_unique_task
from loguru import logger

from .crud import db
from .tasks import wait_for_paid_invoices
from .views import reverse_proxy_generic_router
from .views_api import reverse_proxy_api_router

reverse_proxy_ext: APIRouter = APIRouter(
    prefix="/reverse_proxy", tags=["reverse_proxy"]
)
reverse_proxy_ext.include_router(reverse_proxy_generic_router)
reverse_proxy_ext.include_router(reverse_proxy_api_router)


reverse_proxy_static_files = [
    {
        "path": "/reverse_proxy/static",
        "name": "reverse_proxy_static",
    }
]

scheduled_tasks: list[asyncio.Task] = []


def reverse_proxy_stop():
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)


def reverse_proxy_start():
    task = create_permanent_unique_task("ext_reverse_proxy", wait_for_paid_invoices)
    scheduled_tasks.append(task)


__all__ = [
    "db",
    "reverse_proxy_ext",
    "reverse_proxy_start",
    "reverse_proxy_static_files",
    "reverse_proxy_stop",
]