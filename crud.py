# Description: This file contains the CRUD operations for talking to the database.


from lnbits.db import Database, Filters, Page
from lnbits.helpers import urlsafe_short_hash

from .models import (
    Payments,
    PaymentsFilters,
    CreatePayments,
    CreateProxies,
    Proxies,
    ProxiesFilters,
)

db = Database("ext_reverse_proxy")


########################### Proxies ############################
async def create_proxies(user_id: str, data: CreateProxies) -> Proxies:
    proxies = Proxies(**data.dict(), id=urlsafe_short_hash(), user_id=user_id)
    await db.insert("reverse_proxy.proxies", proxies)
    return proxies


async def get_proxies(
    user_id: str,
    proxies_id: str,
) -> Proxies | None:
    return await db.fetchone(
        """
            SELECT * FROM reverse_proxy.proxies
            WHERE id = :id AND user_id = :user_id
        """,
        {"id": proxies_id, "user_id": user_id},
        Proxies,
    )


async def get_proxies_by_id(
    proxies_id: str,
) -> Proxies | None:
    return await db.fetchone(
        """
            SELECT * FROM reverse_proxy.proxies
            WHERE id = :id
        """,
        {"id": proxies_id},
        Proxies,
    )


async def get_proxies_ids_by_user(
    user_id: str,
) -> list[str]:
    rows: list[dict] = await db.fetchall(
        """
            SELECT DISTINCT id FROM reverse_proxy.proxies
            WHERE user_id = :user_id
        """,
        {"user_id": user_id},
    )

    return [row["id"] for row in rows]


async def get_proxies_paginated(
    user_id: str | None = None,
    filters: Filters[ProxiesFilters] | None = None,
) -> Page[Proxies]:
    where = []
    values = {}
    if user_id:
        where.append("user_id = :user_id")
        values["user_id"] = user_id

    return await db.fetch_page(
        "SELECT * FROM reverse_proxy.proxies",
        where=where,
        values=values,
        filters=filters,
        model=Proxies,
    )


async def update_proxies(data: Proxies) -> Proxies:
    await db.update("reverse_proxy.proxies", data)
    return data


async def delete_proxies(user_id: str, proxies_id: str) -> None:
    await db.execute(
        """
            DELETE FROM reverse_proxy.proxies
            WHERE id = :id AND user_id = :user_id
        """,
        {"id": proxies_id, "user_id": user_id},
    )


################################# Payments ###########################


async def create_payments(proxies_id: str, data: CreatePayments) -> Payments:
    payments = Payments(**data.dict(), id=urlsafe_short_hash(), proxies_id=proxies_id)
    await db.insert("reverse_proxy.payments", payments)
    return payments


async def get_payments(
    proxies_id: str,
    payments_id: str,
) -> Payments | None:
    return await db.fetchone(
        """
            SELECT * FROM reverse_proxy.payments
            WHERE id = :id AND proxies_id = :proxies_id
        """,
        {"id": payments_id, "proxies_id": proxies_id},
        Payments,
    )


async def get_payments_by_id(
    payments_id: str,
) -> Payments | None:
    return await db.fetchone(
        """
            SELECT * FROM reverse_proxy.payments
            WHERE id = :id
        """,
        {"id": payments_id},
        Payments,
    )


async def get_payments_paginated(
    proxies_ids: list[str] | None = None,
    filters: Filters[PaymentsFilters] | None = None,
) -> Page[Payments]:

    if not proxies_ids:
        return Page(data=[], total=0)

    where = []
    values = {}
    id_clause = []
    for i, item_id in enumerate(proxies_ids):
        # proxies_ids are not user input, but DB entries, so this is safe
        proxies_id = f"proxies_id__{i}"
        id_clause.append(f"proxies_id = :{proxies_id}")
        values[proxies_id] = item_id
    or_clause = " OR ".join(id_clause)
    where.append(f"({or_clause})")

    return await db.fetch_page(
        "SELECT * FROM reverse_proxy.payments",
        where=where,
        values=values,
        filters=filters,
        model=Payments,
    )


async def update_payments(data: Payments) -> Payments:
    await db.update("reverse_proxy.payments", data)
    return data


async def delete_payments(proxies_id: str, payments_id: str) -> None:
    await db.execute(
        """
            DELETE FROM reverse_proxy.payments
            WHERE id = :id AND proxies_id = :proxies_id
        """,
        {"id": payments_id, "proxies_id": proxies_id},
    )


