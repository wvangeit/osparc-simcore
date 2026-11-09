from aiohttp import web
from aiohttp_security.api import forget, remember
from models_library.emails import LowerCaseEmailStr
from models_library.users import UserID

from ._identity_policy import remove_other_user_sessions

# Identification *string* for an authenticated user
type IdentityStr = str


async def remember_identity(
    request: web.Request,
    response: web.Response,
    *,
    user_email: LowerCaseEmailStr,
    user_id: UserID,
) -> web.Response:
    """Remember = Saves verified identify in current session"""
    await remember(
        request=request,
        response=response,
        identity=user_email,
        user_id=user_id,
    )
    return response


async def forget_identity(request: web.Request, response: web.Response) -> web.Response:
    """Forget = Drops verified identity stored in current session"""
    await forget(request, response)
    return response


async def forget_other_user_sessions(request: web.Request, *, user_id: UserID) -> None:
    """Logs out all the sessions of the user except the current one"""
    await remove_other_user_sessions(request, user_id=user_id)
