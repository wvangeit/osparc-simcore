# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable
# pylint: disable=too-many-arguments

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from faker import Faker
from pytest_simcore.helpers.faker_factories import random_product
from servicelib.aiohttp import status
from servicelib.rest_constants import X_PRODUCT_NAME_HEADER
from simcore_service_webserver.products import products_web
from simcore_service_webserver.products._web_events import _set_app_state
from simcore_service_webserver.products._web_middlewares import (
    discover_product_middleware,
)
from simcore_service_webserver.products.models import Product
from simcore_service_webserver.statics._constants import FRONTEND_APP_DEFAULT
from yarl import URL


@pytest.fixture
def mock_product_db_get_data(
    faker: Faker, product_db_server_defaults: dict[str, Any]
) -> list[dict[str, Any]]:

    _SUBDOMAIN_PREFIX = r"[\w-]+\."

    return [
        random_product(
            name="osparc",
            host_regex=rf"^({_SUBDOMAIN_PREFIX})*osparc[\.-]",
            fake=faker,
            **product_db_server_defaults,
        ),
        random_product(
            name="s4l",
            host_regex=rf"^({_SUBDOMAIN_PREFIX})*(s4l|sim4life)[\.-]",
            fake=faker,
            **product_db_server_defaults,
        ),
        random_product(
            name="tis",
            host_regex=rf"^({_SUBDOMAIN_PREFIX})*(tis|^ti-solutions)[\.-]",
            fake=faker,
            vendor={
                "name": "ACME",
                "address": "sesame street",
                "copyright": "© ACME correcaminos",
                "url": "https://acme.com",
                "forum_url": "https://forum.acme.com",
            },
            **product_db_server_defaults,
        ),
    ]


@pytest.fixture
def mock_app(mock_product_db_get_data: list[dict[str, Any]]) -> web.Application:
    app = web.Application()

    app_products: dict[str, Product] = {
        product_db_get["name"]: Product.model_validate(product_db_get)
        for product_db_get in mock_product_db_get_data
    }

    default_product_name = next(iter(app_products.keys()))
    _set_app_state(app, app_products, default_product_name)

    return app


@pytest.mark.parametrize(
    "request_url,x_product_name_header,expected_product",
    [
        ("https://tis-master.domain.io/", "tis", "tis"),
        ("https://osparc-master.domain.com/v0/projects", None, "osparc"),
        ("https://s4l.domain.com/", "s4l", "s4l"),
        ("https://some-valid-but-undefined-product.io/", None, FRONTEND_APP_DEFAULT),
        ("https://sim4life.io/", "s4l", "s4l"),
        ("https://api.sim4life.io/", None, "s4l"),  # e.g. api client is not set
        ("https://ti-solutions.io/", "tis", "tis"),
        ("https://osparc.io/", None, "osparc"),  # e.g. an old front-end
        ("https://staging.osparc.io/", "osparc", "osparc"),
        ("https://s4l-staging.domain.com/v0/", "s4l", "s4l"),
        # new auth of subdomains. SEE https://github.com/ITISFoundation/osparc-simcore/pull/6484
        (
            "https://34c878cd-f801-433f-9ddb-7dccba9251af.services.s4l-staging.domain.com",
            None,
            "s4l",
        ),
    ],
)
async def test_middleware_product_discovery(
    request_url: str,
    x_product_name_header: str | None,
    expected_product: str,
    mock_app: web.Application,
):
    """
    A client's request reaches the middleware with
        - an url (request_url),
        - a product name in the header from client (product_from_client)
    """
    url = URL(request_url)
    headers = {
        "Host": url.host,
        "X-Forwarded-Host": url.host,
    }
    if x_product_name_header:
        headers.update({X_PRODUCT_NAME_HEADER: x_product_name_header})

    mock_request = make_mocked_request(
        "GET",
        url.path,
        headers=headers,
        app=mock_app,
    )

    async def _mock_handler(_request: web.Request):
        return web.Response(text="OK")

    # run middleware
    response = await discover_product_middleware(mock_request, _mock_handler)

    # checks
    assert products_web.get_product_name(mock_request) == expected_product
    assert response.status == status.HTTP_200_OK
