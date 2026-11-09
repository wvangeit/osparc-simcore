"""Server-side revocation of cookie sessions

Sessions live in an encrypted cookie held by the client, therefore a copy of the
cookie stays valid unless it is revoked here. Each session carries a random id and
its issue time. A session is rejected if
  - it is older than the maximum session lifetime,
  - its id was revoked (e.g. logout), or
  - it was issued before the user's sessions were revoked (e.g. password change).
"""

import time
from typing import Final

from aiohttp import web

from ..session.settings import get_plugin_settings as get_session_settings
from . import _session_revocation_repository

# Used when the session cookie has no max-age (i.e. lives until the browser closes)
_DEFAULT_MAX_SESSION_AGE_SECS: Final[int] = 7 * 24 * 60 * 60


def _get_max_session_age_secs(app: web.Application) -> int:
    return get_session_settings(app).SESSION_COOKIE_MAX_AGE or _DEFAULT_MAX_SESSION_AGE_SECS


async def revoke_session(app: web.Application, *, session_id: str) -> None:
    await _session_revocation_repository.revoke_session(
        app, session_id=session_id, ttl_secs=_get_max_session_age_secs(app)
    )


async def revoke_user_sessions(app: web.Application, *, user_email: str) -> None:
    """Revokes all sessions of the user issued up to now"""
    await _session_revocation_repository.revoke_sessions_issued_before(
        app,
        user_email=user_email,
        timestamp=time.time(),
        ttl_secs=_get_max_session_age_secs(app),
    )


async def is_session_valid(
    app: web.Application,
    *,
    user_email: str,
    session_id: str | None,
    issued_at: float | None,
) -> bool:
    """

    NOTE: sessions created before revocation was introduced have neither id nor issue time:
    they cannot be revoked individually but are revoked with all the user's sessions.
    """
    if issued_at is not None and time.time() - issued_at > _get_max_session_age_secs(app):
        return False

    session_revoked, revoked_before = await _session_revocation_repository.get_revocations(
        app, session_id=session_id, user_email=user_email
    )
    if session_revoked:
        return False

    return revoked_before is None or (issued_at or 0.0) >= revoked_before
