"""Admin authentication helper endpoints."""

from fastapi import APIRouter, Depends, Request

from kohakuhub.auth.admin_client import forwarded_for, resolve_client
from kohakuhub.auth.admin_token import verify_admin_token
from kohakuhub.config import cfg

router = APIRouter()


@router.get("/auth/client")
async def get_auth_client(request: Request, _admin: bool = Depends(verify_admin_token)):
    """How the server sees the caller, for checking ``ADMIN_AUTH_TRUSTED_PROXY_HOPS``.

    The token throttle counts failures per ``client``. Behind proxies that key is only right when the setting matches
    the number of proxies in front of the API. ``forwarded_for`` is what the proxies wrote, left to right, and ``peer``
    the peer address the application sees (the nearest proxy; uvicorn rewrites it from the header only for peers in
    ``FORWARDED_ALLOW_IPS``, ``127.0.0.1`` by default): count from the right of ``forwarded_for`` to your own address, that count is
    the setting.
    """
    hops = cfg.admin.auth_trusted_proxy_hops
    identity = resolve_client(request, hops)
    return {
        "client": identity.key,
        "address": identity.address,
        "trusted": identity.trusted,
        "hops": hops,
        "forwarded_for_seen": bool(request.headers.getlist("x-forwarded-for")),
        "forwarded_for": forwarded_for(request),
        "peer": getattr(request.client, "host", None),
    }
