"""Browser sessions of the admin panel: the shared admin token exchanged once for an HttpOnly cookie.

``POST /session`` takes the token (through the same throttle as the header) and sets the cookie; ``GET /session`` lets
the panel restore itself after a reload; ``DELETE /session`` signs out. The list and the two ways to end other sessions
need the panel to be signed in, by cookie or by the token header. A script keeps using ``X-Admin-Token``.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from kohakuhub.auth import admin_session
from kohakuhub.auth.admin_token import check_admin_session, check_admin_token, raise_for, verify_admin_token
from kohakuhub.config import cfg
from kohakuhub.db import AdminSession
from kohakuhub.api.utils.accounts import utc_iso

router = APIRouter()


class LoginPayload(BaseModel):
    token: str


def _enabled() -> None:
    # What an older backend answers, so the panel falls back to the token the same way
    if not cfg.admin.session_enabled:
        raise HTTPException(404, detail={"error": "Admin sessions are not enabled"})


def _ends(row: AdminSession) -> dict:
    return {"expires_at": utc_iso(row.expires_at), "absolute_expires_at": utc_iso(row.absolute_expires_at)}


def _current(request: Request) -> AdminSession | None:
    return getattr(request.state, "admin_session", None)


@router.post("/session", status_code=201)
async def sign_in(payload: LoginPayload, request: Request, response: Response):
    """Exchange the shared token for a session cookie."""
    _enabled()
    if (problem := admin_session.csrf_problem(request)) is not None:
        raise HTTPException(403, detail={"error": problem})
    raise_for(check_admin_token(request, payload.token))
    session_id, row = admin_session.create(request)
    admin_session.set_cookie(response, session_id, row.absolute_expires_at)
    return _ends(row)


@router.get("/session")
async def restore(request: Request):
    """The session the cookie stands for (the panel after a reload), or 401."""
    _enabled()
    raise_for(check_admin_session(request))
    return _ends(_current(request))


@router.delete("/session")
async def sign_out(request: Request, response: Response):
    _enabled()
    raise_for(check_admin_session(request))
    admin_session.revoke(_current(request))
    admin_session.clear_cookie(response)
    return {"revoked": True}


@router.get("/admin-sessions")
async def list_sessions(request: Request, _admin: bool = Depends(verify_admin_token)):
    current = _current(request)
    return {
        "sessions": [
            {
                "id": row.id,
                "created_at": utc_iso(row.created_at),
                "last_used_at": utc_iso(row.last_used_at),
                **_ends(row),
                "ip": row.ip,
                "user_agent": row.user_agent,
                "current": current is not None and row.id == current.id,
            }
            for row in admin_session.active_sessions()
        ]
    }


@router.post("/admin-sessions/revoke-others")
async def revoke_others(request: Request, _admin: bool = Depends(verify_admin_token)):
    """End every session but the one asking (all of them when the caller uses the token header)."""
    current = _current(request)
    return {"revoked": admin_session.revoke_others(current.id if current else 0)}


@router.delete("/admin-sessions/{session_id}")
async def revoke_session(session_id: int, request: Request, response: Response, _admin: bool = Depends(verify_admin_token)):
    row = next((row for row in admin_session.active_sessions() if row.id == session_id), None)
    if row is None:
        raise HTTPException(404, detail={"error": "No such active session"})
    admin_session.revoke(row)
    current = _current(request)
    if current is not None and current.id == row.id:
        admin_session.clear_cookie(response)  # the panel just ended its own session
    return {"revoked": True}
