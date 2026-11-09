"""Redis storage of the active login sessions

The session cookie is stateless (encrypted cookie). Each login additionally registers
its session id here, and a session id that is not registered is not accepted.

NOTE: stored in the same Redis DB as the user sessions of the resource manager, grouped
per user_id as its keys are (a dedicated DB would require changing the redis service
configuration). The keys do not end in `:resources` or `:alive`, which are the ones
that the resource manager scans for.

    user_id=<user_id>:login_session_id=<session_id>     (expires with the session)
"""

import contextlib
from collections.abc import Iterator

from aiohttp import web
from models_library.users import UserID
from redis.exceptions import RedisError

from ..redis import get_redis_resources_client
from ._constants import MSG_AUTH_NOT_AVAILABLE


def _session_key(user_id: UserID, session_id: str) -> str:
    return f"user_id={user_id}:login_session_id={session_id}"


@contextlib.contextmanager
def _handle_redis_errors_as_503() -> Iterator[None]:
    try:
        yield
    except RedisError as err:
        raise web.HTTPServiceUnavailable(text=MSG_AUTH_NOT_AVAILABLE) from err


async def add_session(app: web.Application, *, user_id: UserID, session_id: str, ttl_secs: int) -> None:
    with _handle_redis_errors_as_503():
        await get_redis_resources_client(app).set(_session_key(user_id, session_id), 1, ex=ttl_secs)


async def session_exists(app: web.Application, *, user_id: UserID, session_id: str) -> bool:
    with _handle_redis_errors_as_503():
        return bool(await get_redis_resources_client(app).exists(_session_key(user_id, session_id)))


async def delete_session(app: web.Application, *, user_id: UserID, session_id: str) -> None:
    with _handle_redis_errors_as_503():
        await get_redis_resources_client(app).delete(_session_key(user_id, session_id))


async def delete_user_sessions(app: web.Application, *, user_id: UserID, keep_session_id: str | None = None) -> None:
    kept_key = _session_key(user_id, keep_session_id) if keep_session_id else None
    with _handle_redis_errors_as_503():
        client = get_redis_resources_client(app)
        keys = [key async for key in client.scan_iter(match=_session_key(user_id, "*")) if key != kept_key]
        if keys:
            await client.delete(*keys)
