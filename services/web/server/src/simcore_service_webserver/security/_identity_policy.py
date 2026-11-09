import secrets
from typing import Final

from aiohttp import web
from aiohttp_security.session_identity import SessionIdentityPolicy
from aiohttp_session import get_session
from models_library.users import UserID

from . import _login_session_service

SESSION_ID_KEY: Final[str] = "AIOHTTP_SECURITY_SESSION_ID"
SESSION_USER_ID_KEY: Final[str] = "AIOHTTP_SECURITY_SESSION_USER_ID"

# identify is called several times per request. Caches the validation result per request
_REQUEST_VALID_IDENTITY_KEY: Final[str] = f"{__name__}.valid_identity"


class RegisteredSessionIdentityPolicy(SessionIdentityPolicy):
    """Session identity that requires the login session to be registered server-side

    SEE _login_session_service
    """

    async def identify(self, request: web.Request) -> str | None:
        identity: str | None = await super().identify(request)
        if identity is None:
            return None

        if request.get(_REQUEST_VALID_IDENTITY_KEY) == identity:
            return identity

        session = await get_session(request)
        if not await _login_session_service.is_session_active(
            request.app, user_id=session.get(SESSION_USER_ID_KEY), session_id=session.get(SESSION_ID_KEY)
        ):
            return None

        request[_REQUEST_VALID_IDENTITY_KEY] = identity
        return identity

    async def remember(
        self,
        request: web.Request,
        response: web.StreamResponse,
        identity: str,
        *,
        user_id: UserID | None = None,
        **kwargs,
    ) -> None:
        # NOTE: required by security_web.remember_identity
        if user_id is None:
            msg = "user_id is required to register the login session"
            raise ValueError(msg)

        await super().remember(request, response, identity, **kwargs)

        # every login has its own session id
        session = await get_session(request)
        await self._remove_registered_session(request)

        session_id = secrets.token_urlsafe(16)
        await _login_session_service.register_session(request.app, user_id=user_id, session_id=session_id)
        session[SESSION_ID_KEY] = session_id
        session[SESSION_USER_ID_KEY] = user_id
        request.pop(_REQUEST_VALID_IDENTITY_KEY, None)

    async def forget(self, request: web.Request, response: web.StreamResponse) -> None:
        await self._remove_registered_session(request)
        request.pop(_REQUEST_VALID_IDENTITY_KEY, None)

        await super().forget(request, response)

    @staticmethod
    async def _remove_registered_session(request: web.Request) -> None:
        session = await get_session(request)
        session_id = session.pop(SESSION_ID_KEY, None)
        user_id = session.pop(SESSION_USER_ID_KEY, None)
        if session_id is not None and user_id is not None:
            await _login_session_service.remove_session(request.app, user_id=user_id, session_id=session_id)


async def remove_other_user_sessions(request: web.Request, *, user_id: UserID) -> None:
    """Removes all the sessions of the user except the one of this request"""
    session = await get_session(request)
    await _login_session_service.remove_user_sessions(
        request.app, user_id=user_id, keep_session_id=session.get(SESSION_ID_KEY)
    )
