from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

from .services import cleanup_expired_proxies_and_sync_caddy

reverse_proxy_generic_router = APIRouter()


def reverse_proxy_renderer():
    return template_renderer(["reverse_proxy/templates"])


@reverse_proxy_generic_router.get("/", response_class=HTMLResponse)
async def index(req: Request, user: User = Depends(check_user_exists)):
    # Opportunistically clean up expired tunnels and sync Caddy
    await cleanup_expired_proxies_and_sync_caddy()

    return reverse_proxy_renderer().TemplateResponse(
        "reverse_proxy/index.html", {"request": req, "user": user.json()}
    )
