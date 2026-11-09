"""Redis storage of revoked (otherwise stateless) cookie sessions

NOTE: stored in the same Redis DB as the user sessions of the resource manager
(a dedicated DB would require changing the redis service configuration). The key prefix
keeps these keys apart from the registry's `user_id=...:resources|alive` keys.
"""

import contextlib
from collections.abc import Iterator

from aiohttp import web
from redis.exceptions import RedisError

from ..redis import get_redis_resources_client
from ._constants import MSG_AUTH_NOT_AVAILABLE

_KEY_PREFIX = "session-revocation"


def _session_key(session_id: str) -> str:
    return f"{_KEY_PREFIX}:session:{session_id}"


def _user_key(user_email: str) -> str:
    return f"{_KEY_PREFIX}:user:{user_email.lower()}"


@contextlib.contextmanager
def _handle_redis_errors_as_503() -> Iterator[None]:
    try:
        yield
    except RedisError as err:
        raise web.HTTPServiceUnavailable(text=MSG_AUTH_NOT_AVAILABLE) from err


async def revoke_session(app: web.Application, *, session_id: str, ttl_secs: int) -> None:
    with _handle_redis_errors_as_503():
        await get_redis_resources_client(app).set(_session_key(session_id), 1, ex=ttl_secs)


async def revoke_sessions_issued_before(
    app: web.Application, *, user_email: str, timestamp: float, ttl_secs: int
) -> None:
    with _handle_redis_errors_as_503():
        await get_redis_resources_client(app).set(_user_key(user_email), repr(timestamp), ex=ttl_secs)


async def get_revocations(
    app: web.Application, *, session_id: str | None, user_email: str
) -> tuple[bool, float | None]:
    """Returns (session was revoked, user's sessions issued before this timestamp are revoked)"""
    keys = [_user_key(user_email)]
    if session_id:
        keys.append(_session_key(session_id))

    with _handle_redis_errors_as_503():
        values = await get_redis_resources_client(app).mget(keys)

    revoked_before = float(values[0]) if values[0] is not None else None
    session_revoked = len(values) > 1 and values[1] is not None
    return session_revoked, revoked_before
