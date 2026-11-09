# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable
# pylint: disable=too-many-arguments

import fnmatch
import statistics
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from aiohttp.web import RouteTableDef
from aiohttp_session import get_session
from models_library.emails import LowerCaseEmailStr
from models_library.products import ProductName
from models_library.users import UserID
from pydantic import TypeAdapter
from pytest_mock import MockerFixture, MockType
from pytest_simcore.helpers.typing_env import EnvVarsDict
from servicelib.aiohttp import status
from simcore_postgres_database.models.products import LOGIN_SETTINGS_DEFAULT, products
from simcore_postgres_database.models.users import UserRole
from simcore_service_webserver._meta import API_VTAG
from simcore_service_webserver.login.decorators import login_required
from simcore_service_webserver.products._web_events import _set_app_state
from simcore_service_webserver.products._web_middlewares import (
    discover_product_middleware,
)
from simcore_service_webserver.products.models import Product
from simcore_service_webserver.security._login_session_service import is_session_active
from simcore_service_webserver.security.decorators import permission_required
from simcore_service_webserver.security.plugin import setup_security
from simcore_service_webserver.security.security_service import (
    clean_auth_policy_cache,
    remove_user_sessions,
)
from simcore_service_webserver.security.security_web import (
    check_user_authorized,
    forget_identity,
    forget_other_user_sessions,
    remember_identity,
)
from simcore_service_webserver.session.settings import SessionSettings


class FakeRedisForLoginSessions:
    """Minimal in-memory stand-in for the redis client used to store the login sessions"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value, ex: int | None = None) -> None:
        self.data[key] = f"{value}"
        self.ttls[key] = ex

    async def exists(self, key: str) -> int:
        return int(key in self.data)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)
            self.ttls.pop(key, None)

    async def scan_iter(self, match: str):
        for key in list(self.data):
            if fnmatch.fnmatchcase(key, match):
                yield key


@pytest.fixture
def fake_redis_for_login_sessions(mocker: MockerFixture) -> FakeRedisForLoginSessions:
    fake = FakeRedisForLoginSessions()
    mocker.patch(
        "simcore_service_webserver.security._login_session_repository.get_redis_resources_client",
        autospec=True,
        return_value=fake,
    )
    return fake


# Prototype concept -------------------------------------------------------
#
# - remember/forget are verbs that refers to stateful sessions
# - get borrowed from dict/maps
#
_SESSION_PRODUCT_NAME_KEY = "product_name"


async def _remember_product_name(request: web.Request, product: ProductName):
    session = await get_session(request)
    session[_SESSION_PRODUCT_NAME_KEY] = product


async def _get_product_name(request: web.Request) -> ProductName | None:
    session = await get_session(request)
    if product_name := session.get(_SESSION_PRODUCT_NAME_KEY, None):
        return product_name

    # NOTE: this or deduce from url
    raise web.HTTPUnauthorized(text="Invalid session. Reload / first")


async def _forget_product_name(request: web.Request) -> ProductName | None:
    session = await get_session(request)
    return session.pop(_SESSION_PRODUCT_NAME_KEY, None)


async def _remove_user_sessions(request: web.Request):
    await check_user_authorized(request)
    body = await request.json()
    if body.get("keep_current"):
        await forget_other_user_sessions(request, user_id=body["user_id"])
    else:
        await remove_user_sessions(request.app, user_id=body["user_id"])
    return web.json_response(status=status.HTTP_200_OK)


# ------------------------------------------------------------------------


@pytest.fixture
def set_products_in_app_state() -> Callable[[web.Application, OrderedDict[str, Product]], None]:
    """
    Add products in app's state to avoid setting up a full database in tests

        app: web.Application,
        app_products: OrderedDict[str, Product] with the first product being the default
    """

    def _(
        app: web.Application,
        app_products: OrderedDict[str, Product],
    ) -> None:
        first_product = next(iter(app_products.keys()))
        return _set_app_state(app, app_products, default_product_name=first_product)

    return _


@pytest.fixture
def expected_product_name():
    return "tis"


@pytest.fixture
def app_products(expected_product_name: ProductName) -> OrderedDict[str, Product]:
    column_defaults: dict[str, Any] = {
        c.name: f"{c.server_default.arg}"  # pyright: ignore[reportAttributeAccessIssue]
        for c in products.columns
        if c.server_default
    }
    column_defaults["login_settings"] = LOGIN_SETTINGS_DEFAULT

    pp: OrderedDict[str, Product] = OrderedDict()
    pp["tis"] = Product.model_validate(
        {"name": "tis", "host_regex": "tis", "base_url": "https://tip.io", **column_defaults}
    )
    pp["osparc"] = Product.model_validate(
        {"name": "osparc", "host_regex": "osparc", "base_url": "https://osparc.io", **column_defaults}
    )
    pp["s4l"] = Product.model_validate(
        {"name": "s4l", "host_regex": "s4l", "base_url": "https://s4l.io", **column_defaults}
    )

    assert expected_product_name in pp
    return pp


@pytest.fixture
def registered_users(expected_product_name: str):
    users = [
        {
            "email": "foo@email.com",
            "id": 1,
            "role": UserRole.ADMIN,
            "registered_products": {"osparc", expected_product_name},
        },
        {
            "email": "bar@email.com",
            "id": 2,
            "role": UserRole.ADMIN,
            "registered_products": {"osparc"},
        },
    ]
    return {u["email"]: u for u in users}


@pytest.fixture
def app_routes(
    expected_product_name: ProductName,
    app_products: OrderedDict[str, Product],
    registered_users: dict,
) -> RouteTableDef:
    assert API_VTAG == "v0"

    routes = RouteTableDef()

    @routes.get("/")
    async def _init(request: web.Request):
        # get url and deliver product
        product_name = expected_product_name
        await _remember_product_name(request, product_name)
        return web.json_response(status=status.HTTP_200_OK)

    @routes.post("/v0/hack/{product_name}")
    async def _set_other_product(request: web.Request):
        await _remember_product_name(request, request.match_info["product_name"])
        return web.json_response(status=status.HTTP_200_OK)

    @routes.post("/v0/login")
    async def _login(request: web.Request):
        product_name = await _get_product_name(request)
        body = await request.json()
        email = TypeAdapter(LowerCaseEmailStr).validate_python(body["email"])

        # Permission in this product: Has user access to product?
        if product_name not in registered_users[email]["registered_products"]:
            raise web.HTTPForbidden(text="no access to this product")

        # Authentication takes place here
        if body.get("password") != "secret":
            raise web.HTTPUnauthorized(text="wrong password")

        # if all good, let's update session with
        return await remember_identity(request, web.HTTPOk(), user_email=email, user_id=registered_users[email]["id"])

    @routes.post("/v0/public")
    async def _public(request: web.Request):
        assert await _get_product_name(request) == expected_product_name
        return web.json_response(status=status.HTTP_200_OK)

    @routes.post("/v0/admin")
    @login_required  # NOTE: same as `await check_user_authorized(request)``
    @permission_required(
        "admin.*"  # NOTE: same as `await check_user_permission(request, "admin.*")``
    )
    async def _admin_only(request: web.Request):
        assert await _get_product_name(request) == expected_product_name
        return web.json_response(status=status.HTTP_200_OK)

    @routes.post("/v0/logout")
    async def _logout(request: web.Request):
        await check_user_authorized(request)
        return await forget_identity(request, web.HTTPOk())

    routes.post("/v0/remove-user-sessions")(_remove_user_sessions)

    return routes


@pytest.fixture
async def client(
    aiohttp_client: Callable[..., Awaitable[TestClient]],
    mocker: MockerFixture,
    app_products: OrderedDict[str, Product],
    set_products_in_app_state: Callable[[web.Application, OrderedDict[str, Product]], None],
    app_routes: RouteTableDef,
    mock_env_devel_environment: EnvVarsDict,
    mocked_db_setup_in_setup_security: MockType,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    app = web.Application()
    app.router.add_routes(app_routes)

    # mocks 'setup_session': patch to avoid setting up all ApplicationSettings
    session_settings = SessionSettings.create_from_envs()
    print(session_settings.model_dump_json(indent=1))
    mocker.patch(
        "simcore_service_webserver.session.plugin.get_plugin_settings",
        autospec=True,
        return_value=session_settings,
    )

    mocker.patch(
        "simcore_service_webserver.security._login_session_service.get_session_settings",
        autospec=True,
        return_value=session_settings,
    )

    assert not mocked_db_setup_in_setup_security.called
    setup_security(app)
    assert mocked_db_setup_in_setup_security.called

    # mocks 'setup_products': patch to avoid database
    set_products_in_app_state(app, app_products)
    app.middlewares.append(discover_product_middleware)

    return await aiohttp_client(app)


@pytest.fixture
async def basic_db_funs_mocked(client: TestClient, mocker: MockerFixture) -> None:
    assert client.app
    # NOTE: this might be a problem with every test since cache is global per process
    await clean_auth_policy_cache(client.app)

    mocker.patch(
        "simcore_service_webserver.security._authz_policy.get_async_engine",
        autospec=True,
    )


async def test_product_in_session(
    client: TestClient,
    expected_product_name: ProductName,
    basic_db_funs_mocked: None,
):
    resp = await client.post("/v0/public")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"

    # inits session by getting front-end
    resp = await client.get("/")
    assert resp.ok, f"error: {await resp.text()}"

    resp = await client.post("/v0/public")
    assert resp.ok, f"error: {await resp.text()}"


@pytest.fixture
def get_active_user_or_none_dbmock(basic_db_funs_mocked: None, mocker: MockerFixture) -> MagicMock:
    return mocker.patch(
        "simcore_service_webserver.security._authz_policy._authz_repository.get_active_user_or_none",
        autospec=True,
        return_value={"email": "foo@email.com", "id": 1, "role": UserRole.ADMIN},
    )


@pytest.fixture
def is_user_in_product_name_dbmock(basic_db_funs_mocked: None, mocker: MockerFixture) -> MagicMock:
    return mocker.patch(
        "simcore_service_webserver.security._authz_policy._authz_repository.is_user_in_product_name",
        autospec=True,
        return_value=True,
    )


async def test_auth_in_session(
    client: TestClient,
    expected_product_name: ProductName,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
):
    # inits session by getting front-end
    resp = await client.get("/")
    assert resp.ok, f"error: {await resp.text()}"

    # login 'bar' (has no access to product)
    resp = await client.post(
        "/v0/login",
        json={
            "email": "bar@email.com",
            "password": "secret",
        },
    )
    assert resp.status == status.HTTP_403_FORBIDDEN

    # login 'foo' (has access to product)
    resp = await client.post(
        "/v0/login",
        json={
            "email": "foo@email.com",
            "password": "secret",
        },
    )
    assert resp.ok

    resp = await client.post("/v0/public")
    assert resp.ok, f"error: {await resp.text()}"
    assert not get_active_user_or_none_dbmock.called

    resp = await client.post("/v0/admin")
    assert resp.ok, f"error: {await resp.text()}"
    assert get_active_user_or_none_dbmock.called

    resp = await client.post("/v0/logout")
    assert resp.ok, f"error: {await resp.text()}"

    # after logout
    resp = await client.post("/v0/public")
    assert resp.ok, f"error: {await resp.text()}"

    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"

    resp = await client.post("/v0/logout")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"


async def test_hack_product_session(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
):
    resp = await client.post("/v0/hack/s4l")
    assert resp.ok

    # not logged in
    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED

    # login 'foo' (w/o access to s4l)
    resp = await client.post(
        "/v0/login",
        json={
            "email": "foo@email.com",
            "password": "secret",
        },
    )
    assert resp.status == status.HTTP_403_FORBIDDEN


@pytest.fixture
async def session_initialized(
    client: TestClient,
    basic_db_funs_mocked: None,
) -> None:
    assert client.app

    # init
    resp = await client.get("/")
    assert resp.ok, f"error: {await resp.text()}"

    # login
    resp = await client.post(
        "/v0/login",
        json={
            "email": "foo@email.com",
            "password": "secret",
        },
    )
    assert resp.ok, f"error: {await resp.text()}"

    await clean_auth_policy_cache(client.app)


async def test_number_of_db_calls_on_handlers_of_auth_decorators(
    client: TestClient,
    session_initialized: None,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
):
    """test overhead of adding @login_required and @permission_required to a handler"""
    assert client.app

    # reference: eval w/o decorators
    resp = await client.post("/v0/public")
    assert resp.ok, f"error: {await resp.text()}"

    # Number of times db functions are called in one request
    assert get_active_user_or_none_dbmock.call_count == 0
    assert is_user_in_product_name_dbmock.call_count == 0

    # under test: eval w/ auth *_required decorators
    resp = await client.post("/v0/admin")

    assert resp.ok, f"error: {await resp.text()}"

    # Number of times db functions are called in one request
    assert get_active_user_or_none_dbmock.call_count == 1
    assert is_user_in_product_name_dbmock.call_count == 1


async def test_time_overhead_on_handlers_of_auth_decorators(
    client: TestClient,
    session_initialized: None,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
):
    async def _req(url_: str) -> int:
        start_ns = time.perf_counter_ns()
        resp = await client.post(url_)
        elapsed_ns = time.perf_counter_ns() - start_ns

        assert resp.ok, f"error: {await resp.text()}"
        return elapsed_ns

    num_of_rounds = 100
    public_elapsed_times = [await _req("/v0/public") for _ in range(num_of_rounds)]
    admin_elapsed_times = [await _req("/v0/admin") for _ in range(num_of_rounds)]

    # SEE https://docs.python.org/3/library/statistics.html#function-details
    # Note The mean is strongly affected by outliers and is not necessarily a typical example of the data points.
    # For a more robust, although less efficient, measure of central tendency, see median().
    ref_elapsed_ns = statistics.median(public_elapsed_times)
    elapsed_ns = statistics.median(admin_elapsed_times)
    elapsed_ratio = elapsed_ns / max(ref_elapsed_ns, 1)

    # NOTE: 150% more wrt reference (basically ~ 2.5x more !!!!!!!!!!!)
    # and this is mocking the access to the database!
    msg = f"got {elapsed_ratio=} from medians {elapsed_ns=}ns and {ref_elapsed_ns=}ns after {num_of_rounds=}"
    print(msg.capitalize())
    assert elapsed_ns < ref_elapsed_ns * (1 + 1.6), msg


async def _login(client: TestClient, email: str = "foo@email.com") -> None:
    resp = await client.get("/")
    assert resp.ok, f"error: {await resp.text()}"
    resp = await client.post("/v0/login", json={"email": email, "password": "secret"})
    assert resp.ok, f"error: {await resp.text()}"


def _get_cookies(client: TestClient):
    return client.session.cookie_jar.filter_cookies(client.make_url("/"))


def _replace_cookies(client: TestClient, cookies) -> None:
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies(cookies, client.make_url("/"))


def _registered_session_keys(fake: FakeRedisForLoginSessions) -> list[str]:
    return [k for k in fake.data if ":login_session_id=" in k]


async def test_login_registers_an_expiring_session(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)

    # grouped per user_id, as the keys of the resource manager
    (session_key,) = _registered_session_keys(fake_redis_for_login_sessions)
    assert session_key.startswith("user_id=1:login_session_id=")
    assert fake_redis_for_login_sessions.ttls[session_key]


async def test_logout_removes_the_session_and_rejects_a_copy_of_the_cookie(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)
    cookies_before_logout = _get_cookies(client)

    resp = await client.post("/v0/admin")
    assert resp.ok, f"error: {await resp.text()}"

    resp = await client.post("/v0/logout")
    assert resp.ok, f"error: {await resp.text()}"

    assert not _registered_session_keys(fake_redis_for_login_sessions)

    # replays the cookie copied before logging out
    _replace_cookies(client, cookies_before_logout)
    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"


async def test_new_login_gets_a_new_session_id(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)
    resp = await client.post("/v0/logout")
    assert resp.ok

    await _login(client)
    resp = await client.post("/v0/admin")
    assert resp.ok, f"error: {await resp.text()}"
    assert len(_registered_session_keys(fake_redis_for_login_sessions)) == 1


async def test_login_again_in_the_same_session_replaces_the_session(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)
    first_cookies = _get_cookies(client)

    await _login(client)
    assert len(_registered_session_keys(fake_redis_for_login_sessions)) == 1

    _replace_cookies(client, first_cookies)
    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"


async def test_removing_other_user_sessions_keeps_the_current_one(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    # first session, e.g. a copied cookie
    await _login(client)
    first_session_cookies = _get_cookies(client)

    # second session, e.g. the user changing the password
    client.session.cookie_jar.clear()
    await _login(client)
    assert len(_registered_session_keys(fake_redis_for_login_sessions)) == 2

    resp = await client.post(
        "/v0/remove-user-sessions",
        json={"user_id": 1, "keep_current": True},
    )
    assert resp.ok, f"error: {await resp.text()}"
    assert len(_registered_session_keys(fake_redis_for_login_sessions)) == 1

    resp = await client.post("/v0/admin")
    assert resp.ok, f"error: {await resp.text()}"

    _replace_cookies(client, first_session_cookies)
    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"


async def test_removing_all_user_sessions_logs_out_the_current_one_too(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)

    resp = await client.post(
        "/v0/remove-user-sessions",
        json={"user_id": 1, "keep_current": False},
    )
    assert resp.ok, f"error: {await resp.text()}"
    assert not _registered_session_keys(fake_redis_for_login_sessions)

    resp = await client.post("/v0/admin")
    assert resp.status == status.HTTP_401_UNAUTHORIZED, f"error: {await resp.text()}"


async def test_removing_sessions_of_a_user_does_not_affect_other_users(
    client: TestClient,
    get_active_user_or_none_dbmock: MagicMock,
    is_user_in_product_name_dbmock: MagicMock,
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
):
    await _login(client)

    resp = await client.post(
        "/v0/remove-user-sessions",
        json={"user_id": 2, "keep_current": False},
    )
    assert resp.ok, f"error: {await resp.text()}"

    resp = await client.post("/v0/admin")
    assert resp.ok, f"error: {await resp.text()}"


@pytest.fixture
def session_settings_mock(mocker: MockerFixture) -> MagicMock:
    settings = MagicMock()
    settings.SESSION_COOKIE_MAX_AGE = 3600
    mocker.patch(
        "simcore_service_webserver.security._login_session_service.get_session_settings",
        autospec=True,
        return_value=settings,
    )
    return settings


_USER_ID = UserID(1)
_OTHER_USER_ID = UserID(2)


async def test_session_without_id_is_accepted(
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
    session_settings_mock: MagicMock,
):
    # e.g. cookies minted by the api-server, which only have the identity
    assert await is_session_active(web.Application(), user_id=None, session_id=None)
    assert await is_session_active(web.Application(), user_id=_USER_ID, session_id=None)


async def test_session_is_only_active_if_registered_for_that_user(
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
    session_settings_mock: MagicMock,
):
    app = web.Application()
    await fake_redis_for_login_sessions.set("user_id=1:login_session_id=abc", 1, ex=60)

    assert await is_session_active(app, user_id=_USER_ID, session_id="abc")
    assert not await is_session_active(app, user_id=_OTHER_USER_ID, session_id="abc")
    assert not await is_session_active(app, user_id=_USER_ID, session_id="unknown")
    assert not await is_session_active(app, user_id=None, session_id="abc")


async def test_removing_user_sessions_does_not_match_other_user_ids(
    fake_redis_for_login_sessions: FakeRedisForLoginSessions,
    session_settings_mock: MagicMock,
):
    app = web.Application()
    for user_id in (1, 11):
        await fake_redis_for_login_sessions.set(f"user_id={user_id}:login_session_id=abc", 1, ex=60)
    # keys of the resource manager are not touched
    await fake_redis_for_login_sessions.set("user_id=1:client_session_id=xyz:alive", 1, ex=60)

    await remove_user_sessions(app, user_id=_USER_ID)

    assert set(fake_redis_for_login_sessions.data) == {
        "user_id=11:login_session_id=abc",
        "user_id=1:client_session_id=xyz:alive",
    }
