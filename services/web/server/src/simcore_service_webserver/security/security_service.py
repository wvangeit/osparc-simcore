"""Service-layer interface

NOTE: Must not include functions that depend on aiohttp.web.Request (use *_web.py instead)
"""

from ._authz_service import (
    check_password,
    clean_auth_policy_cache,
    encrypt_password,
    get_access_model,
    password_needs_rehash,
)
from ._session_revocation_service import revoke_user_sessions

__all__: tuple[str, ...] = (
    "check_password",
    "clean_auth_policy_cache",
    "encrypt_password",
    "get_access_model",
    "password_needs_rehash",
    "revoke_user_sessions",
)

# nopycln: file  # noqa: ERA001
