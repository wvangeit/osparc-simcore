import secrets
import time
from typing import Final

from aiohttp import web
from aiohttp_security.session_identity import SessionIdentityPolicy
from aiohttp_session import get_session

from . import _session_revocation_service

SESSION_ID_KEY: Final[str] = "AIOHTTP_SECURITY_SESSION_ID"
SESSION_ISSUED_AT_KEY: Final[str] = "AIOHTTP_SECURITY_SESSION_ISSUED_AT"

# identify is called several times per request. Caches the validation result per request
_REQUEST_VALID_IDENTITY_KEY: Final[str] = f"{__name__}.valid_identity"


class RevocableSessionIdentityPolicy(SessionIdentityPolicy):
    """Session identity whose sessions can be revoked server-side

    SEE _session_revocation_service
    """

    async def identify(self, request: web.Request) -> str | None:
        identity: str | None = await super().identify(request)
        if identity is None:
            return None

        if request.get(_REQUEST_VALID_IDENTITY_KEY) == identity:
            return identity

        session = await get_session(request)
        if not await _session_revocation_service.is_session_valid(
            request.app,
            user_email=identity,
            session_id=session.get(SESSION_ID_KEY),
            issued_at=session.get(SESSION_ISSUED_AT_KEY),
        ):
            return None

        request[_REQUEST_VALID_IDENTITY_KEY] = identity
        return identity

    async def remember(self, request: web.Request, response: web.StreamResponse, identity: str, **kwargs) -> None:
        await super().remember(request, response, identity, **kwargs)

        # new id for every login
        session = await get_session(request)
        session[SESSION_ID_KEY] = secrets.token_urlsafe(16)
        session[SESSION_ISSUED_AT_KEY] = time.time()
        request.pop(_REQUEST_VALID_IDENTITY_KEY, None)

    async def forget(self, request: web.Request, response: web.StreamResponse) -> None:
        session = await get_session(request)
        if session_id := session.pop(SESSION_ID_KEY, None):
            await _session_revocation_service.revoke_session(request.app, session_id=session_id)
        session.pop(SESSION_ISSUED_AT_KEY, None)
        request.pop(_REQUEST_VALID_IDENTITY_KEY, None)

        await super().forget(request, response)


async def renew_session_issue_time(request: web.Request) -> None:
    """Keeps the current session valid after all the user's sessions were revoked"""
    session = await get_session(request)
    session[SESSION_ISSUED_AT_KEY] = time.time()
