from http import HTTPStatus

from fastapi import APIRouter, Depends
from fastapi.exceptions import HTTPException
from lnbits.core.models import SimpleStatus, User
from lnbits.db import Filters, Page
from lnbits.decorators import (
    check_super_user,
    parse_filters,
)
from lnbits.helpers import generate_filter_params_openapi

from .crud import (
    create_payments,
    create_proxies,
    delete_payments,
    get_extension_settings_public,
    get_payments_by_id,
    get_payments_paginated,
    get_proxies,
    get_proxies_by_client_note,
    get_proxies_by_id,
    get_proxies_ids_by_user,
    get_proxies_paginated,
    update_payments,
    update_proxies,
)
from .models import (
    CreatePayments,
    CreateProxies,
    CreateTunnelRequest,
    CreateTunnelResponse,
    ExtensionSettings,
    Payments,
    PaymentsFilters,
    PaymentsPaymentRequest,
    Proxies,
    ProxiesFilters,
    PublicTopupRequest,
    PublicTunnel,
)
from .services import (
    create_tunnel_with_invoice,
    delete_proxy_and_key,
    get_settings,
    payment_request_for_payments,
    tunnel_payment_status,
    update_settings,
)

proxies_filters = parse_filters(ProxiesFilters)
payments_filters = parse_filters(PaymentsFilters)

reverse_proxy_api_router = APIRouter()


############################ Settings #############################
@reverse_proxy_api_router.get(
    "/api/v1/settings",
    name="Get Settings",
    summary="Get the settings for the current user.",
    response_description="The settings or 404 if not found",
    response_model=ExtensionSettings,
)
async def api_get_settings(
    user: User = Depends(check_super_user),
) -> ExtensionSettings:
    user_id = "admin" if ExtensionSettings.is_admin_only() else user.id
    return await get_settings(user_id)


@reverse_proxy_api_router.put(
    "/api/v1/settings",
    name="Update Settings",
    summary="Update the settings for the current user.",
    response_description="The updated settings.",
    response_model=ExtensionSettings,
)
async def api_update_extension_settings(
    data: ExtensionSettings,
    user: User = Depends(check_super_user),
) -> ExtensionSettings:
    if ExtensionSettings.is_admin_only() and not user.admin:
        raise HTTPException(
            HTTPStatus.FORBIDDEN,
            "Only admins can update settings.",
        )
    user_id = "admin" if ExtensionSettings.is_admin_only() else user.id
    return await update_settings(user_id, data)


############################# Proxies (internal) #############################
@reverse_proxy_api_router.post("/api/v1/proxies", status_code=HTTPStatus.CREATED)
async def api_create_proxies(
    data: CreateProxies,
    user: User = Depends(check_super_user),
) -> Proxies:
    proxies = await create_proxies(user.id, data)
    return proxies


@reverse_proxy_api_router.put(
    "/api/v1/proxies/{proxies_id}",
    status_code=HTTPStatus.CREATED,
)
async def api_update_proxies(
    proxies_id: str,
    data: CreateProxies,
    user: User = Depends(check_super_user),
) -> Proxies:
    proxies = await get_proxies(user.id, proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies not found.")
    if proxies.user_id != user.id:
        raise HTTPException(HTTPStatus.FORBIDDEN, "You do not own this proxies.")
    proxies = await update_proxies(Proxies(**{**proxies.model_dump(), **data.model_dump()}))
    return proxies


@reverse_proxy_api_router.get(
    "/api/v1/proxies/paginated",
    name="Proxies List",
    summary="get paginated list of proxies",
    response_description="list of proxies",
    openapi_extra=generate_filter_params_openapi(ProxiesFilters),
    response_model=Page[Proxies],
)
async def api_get_proxies_paginated(
    user: User = Depends(check_super_user),
    filters: Filters = Depends(proxies_filters),
) -> Page[Proxies]:
    return await get_proxies_paginated(
        user_id=user.id,
        filters=filters,
    )


@reverse_proxy_api_router.get(
    "/api/v1/proxies/{proxies_id}",
    name="Get Proxies",
    summary="Get the proxies with this id.",
    response_description="A proxies row or 404 if not found",
    response_model=Proxies,
)
async def api_get_proxies(
    proxies_id: str,
    user: User = Depends(check_super_user),
) -> Proxies:
    proxies = await get_proxies(user.id, proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies not found.")
    return proxies


@reverse_proxy_api_router.delete(
    "/api/v1/proxies/{proxies_id}",
    name="Delete Proxies",
    summary="Delete the proxies and optionally all its associated payments.",
    response_description="The status of the deletion.",
    response_model=SimpleStatus,
)
async def api_delete_proxies(
    proxies_id: str,
    clear_payments: bool | None = False,
    user: User = Depends(check_super_user),
) -> SimpleStatus:
    settings = await get_settings(user.id)
    await delete_proxy_and_key(user.id, proxies_id, settings)
    if clear_payments is True:
        # TODO: optionally delete all payments associated with this proxy
        pass
    return SimpleStatus(success=True, message="Proxies Deleted")


############################# Payments #############################
@reverse_proxy_api_router.post(
    "/api/v1/payments/{proxies_id}",
    name="Create Payments",
    summary="Create new payments for the specified proxies.",
    response_description="The created payments.",
    response_model=Payments,
    status_code=HTTPStatus.CREATED,
)
async def api_create_payments(
    proxies_id: str,
    data: CreatePayments,
    user: User = Depends(check_super_user),
) -> Payments:
    proxies = await get_proxies(user.id, proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies not found.")

    payments = await create_payments(proxies_id, data)
    return payments


@reverse_proxy_api_router.put(
    "/api/v1/payments/public/{proxies_id}",
    name="Submit new Payments",
    summary="Public: Generate a new payment request (LN invoice) for the specified proxy.",
    response_description="The created payment request.",
    response_model=PaymentsPaymentRequest | None,
)
async def api_submit_public_payments(
    proxies_id: str,
    data: PublicTopupRequest,
) -> PaymentsPaymentRequest | None:
    """
    PUBLIC: clients can use this to top up / extend an existing proxy.
    Uses days to compute the amount server-side.
    """
    if not await get_proxies_by_id(proxies_id):
        raise HTTPException(HTTPStatus.NOT_FOUND, "Tunnel not found.")
    return await payment_request_for_payments(proxies_id, data)


@reverse_proxy_api_router.get(
    "/api/v1/tunnels/client/{client_note}",
    name="Get tunnels by client note",
    summary="Public: List tunnels matching a client_note.",
    response_model=list[PublicTunnel],
)
async def api_get_tunnels_by_client_note(client_note: str) -> list[PublicTunnel]:
    proxies = await get_proxies_by_client_note(client_note)
    result: list[PublicTunnel] = []
    for proxy in proxies:
        settings = await get_extension_settings_public(proxy.public_id)
        domain = getattr(settings, "domain", "")
        ssh_host = getattr(settings, "ssh_host", "")
        public_url = f"https://{proxy.subdomain}.{domain}" if domain else ""
        result.append(
            PublicTunnel(
                tunnel_id=proxy.id,
                subdomain=proxy.subdomain,
                remote_port=proxy.remote_port,
                ssh_user=proxy.ssh_user,
                ssh_host=ssh_host,
                public_url=public_url,
                expires_at=proxy.expires_at,
                status=proxy.status,
                client_note=proxy.client_note,
            )
        )
    return result


@reverse_proxy_api_router.put(
    "/api/v1/payments/{payments_id}",
    name="Update Payments",
    summary="Update the payments with this id.",
    response_description="The updated payments.",
    response_model=Payments,
)
async def api_update_payments(
    payments_id: str,
    data: CreatePayments,
    user: User = Depends(check_super_user),
) -> Payments:
    payments = await get_payments_by_id(payments_id)
    if not payments:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Payments not found.")

    proxies = await get_proxies(user.id, payments.proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies not found.")

    payments = await update_payments(Payments(**{**payments.model_dump(), **data.model_dump()}))
    return payments


@reverse_proxy_api_router.get(
    "/api/v1/payments/paginated",
    name="Payments List",
    summary="get paginated list of payments",
    response_description="list of payments",
    openapi_extra=generate_filter_params_openapi(PaymentsFilters),
    response_model=Page[Payments],
)
async def api_get_payments_paginated(
    user: User = Depends(check_super_user),
    proxies_id: str | None = None,
    filters: Filters = Depends(payments_filters),
) -> Page[Payments]:
    proxies_ids = await get_proxies_ids_by_user(user.id)

    if proxies_id:
        if proxies_id not in proxies_ids:
            raise HTTPException(HTTPStatus.FORBIDDEN, "Not your proxies.")
        proxies_ids = [proxies_id]

    return await get_payments_paginated(
        proxies_ids=proxies_ids,
        filters=filters,
    )


@reverse_proxy_api_router.get(
    "/api/v1/payments/{payments_id}",
    name="Get Payments",
    summary="Get the payments with this id.",
    response_description="A payments row or 404 if not found",
    response_model=Payments,
)
async def api_get_payments(
    payments_id: str,
    user: User = Depends(check_super_user),
) -> Payments:
    payments = await get_payments_by_id(payments_id)
    if not payments:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Payments not found.")
    proxies = await get_proxies(user.id, payments.proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies deleted for this Payments.")
    return payments


@reverse_proxy_api_router.delete(
    "/api/v1/payments/{payments_id}",
    name="Delete Payments",
    summary="Delete the payments",
    response_description="The status of the deletion.",
    response_model=SimpleStatus,
)
async def api_delete_payments(
    payments_id: str,
    user: User = Depends(check_super_user),
) -> SimpleStatus:
    payments = await get_payments_by_id(payments_id)
    if not payments:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Payments not found.")
    proxies = await get_proxies(user.id, payments.proxies_id)
    if not proxies:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Proxies deleted for this Payments.")

    await delete_payments(proxies.id, payments_id)
    return SimpleStatus(success=True, message="Payments Deleted")


#################### Public tunnel request endpoint ####################


@reverse_proxy_api_router.post(
    "/api/v1/tunnels",
    name="Create tunnel + invoice",
    summary="Public: Request a reverse proxy for X days; returns LN invoice and tunnel details.",
    response_model=CreateTunnelResponse,
    status_code=HTTPStatus.CREATED,
)
async def api_create_tunnel_public(
    data: CreateTunnelRequest,
) -> CreateTunnelResponse:
    the_settings = await get_extension_settings_public(data.public_id)
    if not the_settings:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Settings not found.")

    return await create_tunnel_with_invoice(
        req=data,
        settings=the_settings,
        extra={"tag": "reverse_proxy"},
    )


@reverse_proxy_api_router.get("/api/v1/tunnels/{tunnel_id}/status")
async def api_tunnel_payment_status(tunnel_id: str, payment_hash: str) -> dict:
    result = await tunnel_payment_status(tunnel_id, payment_hash)
    if result is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Tunnel invoice not found.")
    return result
