"""Server-side registry of the active login sessions

Sessions live in an encrypted cookie held by the client, therefore a copy of the cookie
stays valid unless the server refuses it. Each login registers a random session id in
Redis and the id in the cookie must stay registered:
  - logout removes its session,
  - password change or reset removes the user's sessions,
  - registered sessions expire with the maximum session lifetime.

NOTE: sessions without id are not checked. These are the ones created before this was
introduced and the ones minted by the api-server, which has to keep acting on behalf of the user.
"""

from typing import Final

from aiohttp import web
from models_library.users import UserID

from ..session.settings import get_plugin_settings as get_session_settings
from . import _login_session_repository

# Used when the session cookie has no max-age (i.e. lives until the browser closes)
_DEFAULT_MAX_SESSION_AGE_SECS: Final[int] = 7 * 24 * 60 * 60


def _get_max_session_age_secs(app: web.Application) -> int:
    return get_session_settings(app).SESSION_COOKIE_MAX_AGE or _DEFAULT_MAX_SESSION_AGE_SECS


async def register_session(app: web.Application, *, user_id: UserID, session_id: str) -> None:
    await _login_session_repository.add_session(
        app,
        user_id=user_id,
        session_id=session_id,
        ttl_secs=_get_max_session_age_secs(app),
    )


async def remove_session(app: web.Application, *, user_id: UserID, session_id: str) -> None:
    await _login_session_repository.delete_session(app, user_id=user_id, session_id=session_id)


async def remove_user_sessions(app: web.Application, *, user_id: UserID, keep_session_id: str | None = None) -> None:
    await _login_session_repository.delete_user_sessions(app, user_id=user_id, keep_session_id=keep_session_id)


async def is_session_active(app: web.Application, *, user_id: UserID | None, session_id: str | None) -> bool:
    if session_id is None:
        return True

    return user_id is not None and await _login_session_repository.session_exists(
        app, user_id=user_id, session_id=session_id
    )
