# pylint:disable=unused-variable
# pylint:disable=unused-argument
# pylint:disable=redefined-outer-name


import pytest
from models_library.products import ProductName
from pytest_simcore.helpers.monkeypatch_envs import setenvs_from_dict
from pytest_simcore.helpers.typing_env import EnvVarsDict
from simcore_service_webserver.constants import FRONTEND_APP_DEFAULT


@pytest.fixture
def default_product_name() -> ProductName:
    return FRONTEND_APP_DEFAULT


@pytest.fixture
def app_environment(
    app_environment: EnvVarsDict,
    monkeypatch: pytest.MonkeyPatch,
):
    return setenvs_from_dict(
        monkeypatch,
        {
            **app_environment,  # WARNING: AFTER env_devel_dict because HOST are set to 127.0.0.1 in here
            "WEBSERVER_DB_LISTENER": "0",
            "WEBSERVER_DEV_FEATURES_ENABLED": "1",
            "WEBSERVER_GARBAGE_COLLECTOR": "null",
        },
    )
