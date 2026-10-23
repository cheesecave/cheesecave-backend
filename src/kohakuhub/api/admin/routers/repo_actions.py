"""The repository actions the admin panel used to send to ``/api/repos/*`` with the token.

The session cookie only travels under ``/admin``, so these three have admin-scoped routes. They are the same handlers as
the public ones, called as the administrator, so nothing about what they do differs.
"""

from fastapi import APIRouter, Depends

from kohakuhub.api.operation_capabilities import require_repository_squash_enabled
from kohakuhub.api.repo.routers.crud import (
    DeleteRepoPayload,
    MoveRepoPayload,
    SquashRepoPayload,
    delete_repo,
    move_repo,
    squash_repo,
)
from kohakuhub.auth.admin_token import verify_admin_token

router = APIRouter()

ADMIN = (None, True)  # the (user, is_admin) the handlers take from get_current_user_or_admin


@router.delete("/repos/delete")
async def delete(payload: DeleteRepoPayload, _admin: bool = Depends(verify_admin_token)):
    return await delete_repo(payload, ADMIN)


@router.post("/repos/move")
async def move(payload: MoveRepoPayload, _admin: bool = Depends(verify_admin_token)):
    return await move_repo(payload, ADMIN)


@router.post("/repos/squash", dependencies=[Depends(require_repository_squash_enabled)])
async def squash(payload: SquashRepoPayload, _admin: bool = Depends(verify_admin_token)):
    return await squash_repo(payload, ADMIN)
