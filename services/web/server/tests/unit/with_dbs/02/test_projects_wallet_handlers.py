# pylint: disable=protected-access
# pylint: disable=redefined-outer-name
# pylint: disable=too-many-arguments
# pylint: disable=unused-argument
# pylint: disable=unused-variable
# pylint: disable=too-many-statements

from collections.abc import Iterator
from decimal import Decimal
from http import HTTPStatus
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from aiohttp.test_utils import TestClient
from models_library.api_schemas_resource_usage_tracker.credit_transactions import (
    WalletTotalCredits,
)
from models_library.api_schemas_resource_usage_tracker.service_runs import (
    ServiceRunPage,
)
from models_library.api_schemas_webserver.wallets import WalletGet
from pytest_mock import MockerFixture
from pytest_simcore.helpers.assert_checks import assert_status
from pytest_simcore.helpers.webserver_login import LoggedUser, UserInfoDict
from servicelib.aiohttp import status
from simcore_postgres_database.models.wallets import wallets
from simcore_service_webserver._meta import api_version_prefix
from simcore_service_webserver.db.models import UserRole
from simcore_service_webserver.projects.models import ProjectDict

API_PREFIX = "/" + api_version_prefix


@pytest.mark.parametrize(
    "user_role,expected",
    [
        (UserRole.ANONYMOUS, status.HTTP_401_UNAUTHORIZED),
        (UserRole.GUEST, status.HTTP_200_OK),
        (UserRole.USER, status.HTTP_200_OK),
        (UserRole.TESTER, status.HTTP_200_OK),
    ],
)
async def test_project_wallets_user_role_access(
    client: TestClient,
    logged_user: UserInfoDict,
    user_project: ProjectDict,
    user_role: UserRole,
    expected: HTTPStatus,
):
    base_url = client.app.router["get_project_wallet"].url_for(
        project_id=user_project["uuid"]
    )
    resp = await client.get(f"{base_url}")
    assert (
        resp.status == status.HTTP_401_UNAUTHORIZED
        if user_role == UserRole.ANONYMOUS
        else status.HTTP_200_OK
    )


@pytest.mark.parametrize("user_role,expected", [(UserRole.USER, status.HTTP_200_OK)])
async def test_project_wallets_user_project_access(
    client: TestClient,
    logged_user: UserInfoDict,
    user_project: ProjectDict,
    expected: HTTPStatus,
    # postgres_db: sa.engine.Engine,
):
    base_url = client.app.router["get_project_wallet"].url_for(
        project_id=user_project["uuid"]
    )
    resp = await client.get(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data is None

    # Now we will log as a different user who doesnt have access to the project
    async with LoggedUser(client):
        base_url = client.app.router["get_project_wallet"].url_for(
            project_id=user_project["uuid"]
        )
        resp = await client.get(f"{base_url}")
        _, errors = await assert_status(resp, status.HTTP_403_FORBIDDEN)
        assert errors


@pytest.fixture()
def setup_wallets_db(
    postgres_db: sa.engine.Engine, logged_user: UserInfoDict
) -> Iterator[list[WalletGet]]:
    with postgres_db.connect() as con:
        output = []
        for name in ["My wallet 1", "My wallet 2"]:
            result = con.execute(
                wallets.insert()
                .values(
                    name=name,
                    owner=logged_user["primary_gid"],
                    status="ACTIVE",
                    product_name="osparc",
                )
                .returning(sa.literal_column("*"))
            )
            output.append(WalletGet.model_validate(result.fetchone()))
        yield output
        con.execute(wallets.delete())


@pytest.fixture
def mock_get_project_wallet_total_credits(
    mocker: MockerFixture, setup_wallets_db: list[WalletGet]
):
    mocker.patch(
        "simcore_service_webserver.projects._wallets_service.credit_transactions.get_project_wallet_total_credits",
        spec=True,
        return_value=WalletTotalCredits(
            wallet_id=setup_wallets_db[0].wallet_id, available_osparc_credits=Decimal(0)
        ),
    )


@pytest.fixture
def mock_get_service_run_page(mocker: MockerFixture):
    mocker.patch(
        "simcore_service_webserver.projects._wallets_service.service_runs.get_service_run_page",
        spec=True,
        return_value=ServiceRunPage(items=[], total=0),
    )


@pytest.mark.parametrize("user_role,expected", [(UserRole.USER, status.HTTP_200_OK)])
async def test_project_wallets_full_workflow(
    client: TestClient,
    logged_user: UserInfoDict,
    user_project: ProjectDict,
    expected: HTTPStatus,
    setup_wallets_db: list[WalletGet],
    mock_get_project_wallet_total_credits: None,
    mock_get_service_run_page: None,
):
    assert client.app

    base_url = client.app.router["get_project_wallet"].url_for(
        project_id=user_project["uuid"]
    )
    resp = await client.get(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data is None

    # Now we will connect the wallet
    base_url = client.app.router["connect_wallet_to_project"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[0].wallet_id}"
    )
    resp = await client.put(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data["walletId"] == setup_wallets_db[0].wallet_id

    base_url = client.app.router["get_project_wallet"].url_for(
        project_id=user_project["uuid"]
    )
    resp = await client.get(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data["walletId"] == setup_wallets_db[0].wallet_id

    # Now we will connect different wallet
    base_url = client.app.router["connect_wallet_to_project"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[1].wallet_id}"
    )
    resp = await client.put(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data["walletId"] == setup_wallets_db[1].wallet_id

    base_url = client.app.router["get_project_wallet"].url_for(
        project_id=user_project["uuid"]
    )
    resp = await client.get(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data["walletId"] == setup_wallets_db[1].wallet_id


@pytest.fixture
def mock_pay_project_debt(mocker: MockerFixture):
    return mocker.patch(
        "simcore_service_webserver.projects._wallets_service.credit_transactions.pay_project_debt",
        spec=True,
    )


@pytest.mark.parametrize("user_role,expected", [(UserRole.USER, status.HTTP_200_OK)])
async def test_pay_project_debt(
    client: TestClient,
    logged_user: UserInfoDict,
    user_project: ProjectDict,
    expected: HTTPStatus,
    setup_wallets_db: list[WalletGet],
    mock_get_project_wallet_total_credits: None,
    mock_get_service_run_page: None,
    mock_pay_project_debt: MagicMock,
):
    assert client.app
    # Use endpoint while project is not connected to any wallet
    base_url = client.app.router["pay_project_debt"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[1].wallet_id}"
    )
    resp = await client.post(f"{base_url}", json={"amount": -100})
    await assert_status(resp, status.HTTP_404_NOT_FOUND)

    # Connect wallet to the project
    base_url = client.app.router["connect_wallet_to_project"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[0].wallet_id}"
    )
    resp = await client.put(f"{base_url}")
    data, _ = await assert_status(resp, expected)
    assert data["walletId"] == setup_wallets_db[0].wallet_id

    # Use endpoint with the same wallet as the one that is connected
    base_url = client.app.router["pay_project_debt"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[0].wallet_id}"
    )
    resp = await client.post(f"{base_url}", json={"amount": -100})
    assert resp.status == status.HTTP_501_NOT_IMPLEMENTED

    # Use endpoint with wrong input
    base_url = client.app.router["pay_project_debt"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[1].wallet_id}"
    )
    resp = await client.post(
        f"{base_url}", json={"amount": 100}
    )  # <-- Error input (must be negative!)
    await assert_status(resp, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Use endpoint properly
    base_url = client.app.router["pay_project_debt"].url_for(
        project_id=user_project["uuid"], wallet_id=f"{setup_wallets_db[1].wallet_id}"
    )
    resp = await client.post(f"{base_url}", json={"amount": -100})
    data, _ = await assert_status(resp, status.HTTP_204_NO_CONTENT)
    assert mock_pay_project_debt.assert_called_once
