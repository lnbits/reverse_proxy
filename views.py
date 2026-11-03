from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from lnbits.core.models import User
from lnbits.decorators import check_super_user
from lnbits.helpers import template_renderer

from .services import cleanup_expired_proxies_and_sync_caddy, get_settings

reverse_proxy_generic_router = APIRouter()


def reverse_proxy_renderer():
    return template_renderer(["reverse_proxy/templates"])


@reverse_proxy_generic_router.get("/", response_class=HTMLResponse)
async def index(req: Request, user: User = Depends(check_super_user)):
    # Opportunistically clean up expired tunnels and sync Caddy
    try:
        settings = await get_settings(user.id)
        await cleanup_expired_proxies_and_sync_caddy(user.id, settings)
    except Exception:
        pass

    return reverse_proxy_renderer().TemplateResponse(
        "reverse_proxy/index.html", {"request": req, "user": user.model_dump_json()}
    )
