from aiohttp import web
from aiohttp_security.api import forget, remember
from models_library.emails import LowerCaseEmailStr

from ._identity_policy import renew_session_issue_time

# Identification *string* for an authenticated user
type IdentityStr = str


async def remember_identity(
    request: web.Request,
    response: web.Response,
    *,
    user_email: LowerCaseEmailStr,
) -> web.Response:
    """Remember = Saves verified identify in current session"""
    await remember(
        request=request,
        response=response,
        identity=user_email,
    )
    return response


async def forget_identity(request: web.Request, response: web.Response) -> web.Response:
    """Forget = Drops verified identity stored in current session"""
    await forget(request, response)
    return response


async def renew_identity(request: web.Request) -> None:
    """Keeps the current session valid after revoking all the user's sessions"""
    await renew_session_issue_time(request)
