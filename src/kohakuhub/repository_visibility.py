"""Resolve HF visibility inputs without silently making an invite repository public."""

from fastapi import HTTPException


def resolve_visibility(
    *,
    private: bool | None = None,
    visibility: str | None = None,
    invite_mode: bool | None = None,
    current_private: bool = False,
    current_invite_mode: bool = False,
) -> tuple[bool, bool]:
    """HF private=true preserves invite mode; explicit public clears it.

    ``visibility`` retains the HF public/private vocabulary. Our separate
    ``invite_mode`` flag enables invitations and implies private=true unless
    the same request explicitly asks for public (which is a conflict).
    Legacy ``private`` precedence is retained for ordinary HF requests.
    """
    if private is None and visibility not in (None, "public", "private"):
        raise HTTPException(400, detail={"error": "Unsupported repository visibility. Only 'public' and 'private' are supported."})
    if invite_mode is True and (private is False or visibility == "public"):
        raise HTTPException(400, detail={"error": "Invitation mode requires a private repository; conflicting public visibility was not applied."})
    requested = private
    if requested is None and visibility is not None:
        requested = visibility == "private"
    if invite_mode is True:
        return True, True
    resolved_private = current_private if requested is None else requested
    resolved_mode = current_invite_mode if invite_mode is None else invite_mode
    return resolved_private, resolved_mode if resolved_private else False
