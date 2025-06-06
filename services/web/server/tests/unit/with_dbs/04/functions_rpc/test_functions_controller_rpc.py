# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient
from common_library.users_enums import UserRole
from models_library.api_schemas_webserver.functions import (
    Function,
    FunctionIDString,
    FunctionJobCollection,
    JSONFunctionInputSchema,
    JSONFunctionOutputSchema,
    ProjectFunction,
    ProjectFunctionJob,
)
from models_library.functions import FunctionJobCollectionsListFilters

# import simcore_service_webserver.functions._functions_controller_rpc as functions_rpc
from models_library.functions_errors import (
    FunctionIDNotFoundError,
    FunctionJobCollectionReadAccessDeniedError,
    FunctionJobIDNotFoundError,
    FunctionJobReadAccessDeniedError,
    FunctionReadAccessDeniedError,
)
from models_library.products import ProductName
from pytest_simcore.helpers.monkeypatch_envs import setenvs_from_dict
from pytest_simcore.helpers.typing_env import EnvVarsDict
from pytest_simcore.helpers.webserver_login import UserInfoDict
from servicelib.rabbitmq import RabbitMQRPCClient
from servicelib.rabbitmq.rpc_interfaces.webserver.functions import (
    functions_rpc_interface as functions_rpc,
)
from settings_library.rabbit import RabbitSettings
from simcore_service_webserver.application_settings import ApplicationSettings

pytest_simcore_core_services_selection = ["rabbit"]


@pytest.fixture
def app_environment(
    rabbit_service: RabbitSettings,
    app_environment: EnvVarsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> EnvVarsDict:
    new_envs = setenvs_from_dict(
        monkeypatch,
        {
            **app_environment,
            "RABBIT_HOST": rabbit_service.RABBIT_HOST,
            "RABBIT_PORT": f"{rabbit_service.RABBIT_PORT}",
            "RABBIT_USER": rabbit_service.RABBIT_USER,
            "RABBIT_SECURE": f"{rabbit_service.RABBIT_SECURE}",
            "RABBIT_PASSWORD": rabbit_service.RABBIT_PASSWORD.get_secret_value(),
        },
    )

    settings = ApplicationSettings.create_from_envs()
    assert settings.WEBSERVER_RABBITMQ

    return new_envs


@pytest.fixture
async def rpc_client(
    rabbitmq_rpc_client: Callable[[str], Awaitable[RabbitMQRPCClient]],
) -> RabbitMQRPCClient:
    return await rabbitmq_rpc_client("client")


@pytest.fixture
def mock_function() -> Function:
    return ProjectFunction(
        title="Test Function",
        description="A test function",
        input_schema=JSONFunctionInputSchema(
            schema_content={
                "type": "object",
                "properties": {"input1": {"type": "string"}},
            }
        ),
        output_schema=JSONFunctionOutputSchema(
            schema_content={
                "type": "object",
                "properties": {"output1": {"type": "string"}},
            }
        ),
        project_id=uuid4(),
        default_inputs=None,
    )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_register_get_delete_function(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    user_role: UserRole,
    osparc_product_name: ProductName,
    other_logged_user: UserInfoDict,
):
    # Register the function
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None
    # Retrieve the function from the repository to verify it was saved
    saved_function = await functions_rpc.get_function(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    with pytest.raises(FunctionReadAccessDeniedError):
        await functions_rpc.get_function(
            rabbitmq_rpc_client=rpc_client,
            function_id=registered_function.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    # Assert the saved function matches the input function
    assert saved_function.uid is not None
    assert saved_function.title == mock_function.title
    assert saved_function.description == mock_function.description

    # Ensure saved_function is of type ProjectFunction before accessing project_id
    assert isinstance(saved_function, ProjectFunction)
    assert saved_function.project_id == mock_function.project_id

    # Assert the returned function matches the expected result
    assert registered_function.title == mock_function.title
    assert registered_function.description == mock_function.description
    assert isinstance(registered_function, ProjectFunction)
    assert registered_function.project_id == mock_function.project_id

    with pytest.raises(FunctionReadAccessDeniedError):
        # Attempt to delete the function by another user
        await functions_rpc.delete_function(
            rabbitmq_rpc_client=rpc_client,
            function_id=registered_function.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    with pytest.raises(FunctionReadAccessDeniedError):
        # Attempt to delete the function by another user
        await functions_rpc.delete_function(
            rabbitmq_rpc_client=rpc_client,
            function_id=registered_function.uid,
            user_id=other_logged_user["id"],
            product_name="this_is_not_osparc",
        )

    # Delete the function using its ID
    await functions_rpc.delete_function(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Attempt to retrieve the deleted function
    with pytest.raises(FunctionIDNotFoundError):
        await functions_rpc.get_function(
            rabbitmq_rpc_client=rpc_client,
            function_id=registered_function.uid,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_get_function_not_found(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
    clean_functions: None,
):
    # Attempt to retrieve a function that does not exist
    with pytest.raises(FunctionIDNotFoundError):
        await functions_rpc.get_function(
            rabbitmq_rpc_client=rpc_client,
            function_id=uuid4(),
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_functions(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
    clean_functions: None,
):
    # List functions when none are registered
    functions, _ = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list is empty
    assert len(functions) == 0

    # Register a function first
    mock_function = ProjectFunction(
        title="Test Function",
        description="A test function",
        input_schema=JSONFunctionInputSchema(),
        output_schema=JSONFunctionOutputSchema(),
        project_id=uuid4(),
        default_inputs=None,
    )
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # List functions
    functions, _ = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the registered function
    assert len(functions) > 0
    assert any(f.uid == registered_function.uid for f in functions)


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_functions_mixed_user(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
    other_logged_user: UserInfoDict,
):
    # Register a function for the logged user
    registered_functions = [
        await functions_rpc.register_function(
            rabbitmq_rpc_client=rpc_client,
            function=mock_function,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        for _ in range(2)
    ]

    # List functions for the other logged user
    other_functions, _ = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=other_logged_user["id"],
        product_name=osparc_product_name,
    )
    # Assert the list contains only the logged user's function
    assert len(other_functions) == 0

    # Register a function for another user
    other_registered_function = [
        await functions_rpc.register_function(
            rabbitmq_rpc_client=rpc_client,
            function=mock_function,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )
        for _ in range(3)
    ]

    # List functions for the logged user
    functions, _ = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    # Assert the list contains only the logged user's function
    assert len(functions) == 2
    assert all(f.uid in [rf.uid for rf in registered_functions] for f in functions)

    other_functions, _ = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=other_logged_user["id"],
        product_name=osparc_product_name,
    )
    # Assert the list contains only the other user's functions
    assert len(other_functions) == 3
    assert all(
        f.uid in [orf.uid for orf in other_registered_function] for f in other_functions
    )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_functions_with_pagination(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    clean_functions: None,
    osparc_product_name: ProductName,
    logged_user: UserInfoDict,
):
    # Register multiple functions
    TOTAL_FUNCTIONS = 3
    for _ in range(TOTAL_FUNCTIONS):
        await functions_rpc.register_function(
            rabbitmq_rpc_client=rpc_client,
            function=mock_function,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )

    functions, page_info = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=2,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # List functions with pagination
    functions, page_info = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=2,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the correct number of functions
    assert len(functions) == 2
    assert page_info.count == 2
    assert page_info.total == TOTAL_FUNCTIONS

    # List the next page of functions
    functions, page_info = await functions_rpc.list_functions(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=2,
        pagination_offset=2,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the correct number of functions
    assert len(functions) == 1
    assert page_info.count == 1
    assert page_info.total == TOTAL_FUNCTIONS


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_update_function_title(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    other_logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # Update the function's title
    updated_title = "Updated Function Title"
    registered_function.title = updated_title
    updated_function = await functions_rpc.update_function_title(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        title=updated_title,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    assert isinstance(updated_function, ProjectFunction)
    assert updated_function.uid == registered_function.uid
    # Assert the updated function's title matches the new title
    assert updated_function.title == updated_title

    # Update the function's title by other user
    updated_title = "Updated Function Title by Other User"
    registered_function.title = updated_title
    with pytest.raises(FunctionReadAccessDeniedError):
        updated_function = await functions_rpc.update_function_title(
            rabbitmq_rpc_client=rpc_client,
            function_id=registered_function.uid,
            title=updated_title,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_update_function_description(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # Update the function's description
    updated_description = "Updated Function Description"
    registered_function.description = updated_description
    updated_function = await functions_rpc.update_function_description(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        description=updated_description,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    assert isinstance(updated_function, ProjectFunction)
    assert updated_function.uid == registered_function.uid
    # Assert the updated function's description matches the new description
    assert updated_function.description == updated_description


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_get_function_input_schema(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # Retrieve the input schema using its ID
    input_schema = await functions_rpc.get_function_input_schema(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the input schema matches the registered function's input schema
    assert input_schema == registered_function.input_schema


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_get_function_output_schema(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # Retrieve the output schema using its ID
    output_schema = await functions_rpc.get_function_output_schema(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the output schema matches the registered function's output schema
    assert output_schema == registered_function.output_schema


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_delete_function(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    other_logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_register_get_delete_function_job(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    other_logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    function_job = ProjectFunctionJob(
        function_uid=registered_function.uid,
        title="Test Function Job",
        description="A test function job",
        project_job_id=uuid4(),
        inputs={"input1": "value1"},
        outputs={"output1": "result1"},
    )

    # Register the function job
    registered_job = await functions_rpc.register_function_job(
        rabbitmq_rpc_client=rpc_client,
        function_job=function_job,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the registered job matches the input job
    assert registered_job.function_uid == function_job.function_uid
    assert registered_job.inputs == function_job.inputs
    assert registered_job.outputs == function_job.outputs

    # Retrieve the function job using its ID
    retrieved_job = await functions_rpc.get_function_job(
        rabbitmq_rpc_client=rpc_client,
        function_job_id=registered_job.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the retrieved job matches the registered job
    assert retrieved_job.function_uid == registered_job.function_uid
    assert retrieved_job.inputs == registered_job.inputs
    assert retrieved_job.outputs == registered_job.outputs

    # Test denied access for another user
    with pytest.raises(FunctionJobReadAccessDeniedError):
        await functions_rpc.get_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=registered_job.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    # Test denied access for anothe product
    with pytest.raises(FunctionJobReadAccessDeniedError):
        await functions_rpc.get_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=registered_job.uid,
            user_id=other_logged_user["id"],
            product_name="this_is_not_osparc",
        )

    with pytest.raises(FunctionJobReadAccessDeniedError):
        # Attempt to delete the function job by another user
        await functions_rpc.delete_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=registered_job.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    # Delete the function job using its ID
    await functions_rpc.delete_function_job(
        rabbitmq_rpc_client=rpc_client,
        function_job_id=registered_job.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Attempt to retrieve the deleted job
    with pytest.raises(FunctionJobIDNotFoundError):
        await functions_rpc.get_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=registered_job.uid,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_get_function_job_not_found(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
    clean_functions: None,
):
    # Attempt to retrieve a function job that does not exist
    with pytest.raises(FunctionJobIDNotFoundError):
        await functions_rpc.get_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=uuid4(),
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_function_jobs(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    function_job = ProjectFunctionJob(
        function_uid=registered_function.uid,
        title="Test Function Job",
        description="A test function job",
        project_job_id=uuid4(),
        inputs={"input1": "value1"},
        outputs={"output1": "result1"},
    )

    # Register the function job
    registered_job = await functions_rpc.register_function_job(
        rabbitmq_rpc_client=rpc_client,
        function_job=function_job,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # List function jobs
    jobs, _ = await functions_rpc.list_function_jobs(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the registered job
    assert len(jobs) > 0
    assert any(j.uid == registered_job.uid for j in jobs)


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_function_jobs_for_functionid(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    first_registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    second_registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    first_registered_function_jobs = []
    second_registered_function_jobs = []
    for i_job in range(6):
        if i_job < 3:
            function_job = ProjectFunctionJob(
                function_uid=first_registered_function.uid,
                title="Test Function Job",
                description="A test function job",
                project_job_id=uuid4(),
                inputs={"input1": "value1"},
                outputs={"output1": "result1"},
            )
            # Register the function job
            first_registered_function_jobs.append(
                await functions_rpc.register_function_job(
                    rabbitmq_rpc_client=rpc_client,
                    function_job=function_job,
                    user_id=logged_user["id"],
                    product_name=osparc_product_name,
                )
            )
        else:
            function_job = ProjectFunctionJob(
                function_uid=second_registered_function.uid,
                title="Test Function Job",
                description="A test function job",
                project_job_id=uuid4(),
                inputs={"input1": "value1"},
                outputs={"output1": "result1"},
            )
            # Register the function job
            second_registered_function_jobs.append(
                await functions_rpc.register_function_job(
                    rabbitmq_rpc_client=rpc_client,
                    function_job=function_job,
                    user_id=logged_user["id"],
                    product_name=osparc_product_name,
                )
            )

    # List function jobs for a specific function ID
    jobs, _ = await functions_rpc.list_function_jobs(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        filter_by_function_id=first_registered_function.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the registered job
    assert len(jobs) > 0
    assert len(jobs) == 3
    assert all(j.function_uid == first_registered_function.uid for j in jobs)


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_function_job_collection(
    client: TestClient,
    mock_function: ProjectFunction,
    rpc_client: RabbitMQRPCClient,
    logged_user: UserInfoDict,
    other_logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    registered_function_job = ProjectFunctionJob(
        function_uid=registered_function.uid,
        title="Test Function Job",
        description="A test function job",
        project_job_id=uuid4(),
        inputs={"input1": "value1"},
        outputs={"output1": "result1"},
    )
    # Register the function job
    function_job_ids = []
    for _ in range(3):
        registered_function_job = ProjectFunctionJob(
            function_uid=registered_function.uid,
            title="Test Function Job",
            description="A test function job",
            project_job_id=uuid4(),
            inputs={"input1": "value1"},
            outputs={"output1": "result1"},
        )
        # Register the function job
        registered_job = await functions_rpc.register_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job=registered_function_job,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        assert registered_job.uid is not None
        function_job_ids.append(registered_job.uid)

    function_job_collection = FunctionJobCollection(
        title="Test Function Job Collection",
        description="A test function job collection",
        job_ids=function_job_ids,
    )

    # Register the function job collection
    registered_collection = await functions_rpc.register_function_job_collection(
        rabbitmq_rpc_client=rpc_client,
        function_job_collection=function_job_collection,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_collection.uid is not None

    # Get the function job collection
    retrieved_collection = await functions_rpc.get_function_job_collection(
        rabbitmq_rpc_client=rpc_client,
        function_job_collection_id=registered_collection.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert retrieved_collection.uid == registered_collection.uid
    assert registered_collection.job_ids == function_job_ids

    # Test denied access for another user
    with pytest.raises(FunctionJobCollectionReadAccessDeniedError):
        await functions_rpc.get_function_job_collection(
            rabbitmq_rpc_client=rpc_client,
            function_job_collection_id=registered_collection.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    # Test denied access for another product
    with pytest.raises(FunctionJobCollectionReadAccessDeniedError):
        await functions_rpc.get_function_job_collection(
            rabbitmq_rpc_client=rpc_client,
            function_job_collection_id=registered_collection.uid,
            user_id=other_logged_user["id"],
            product_name="this_is_not_osparc",
        )

    # Attempt to delete the function job collection by another user
    with pytest.raises(FunctionJobCollectionReadAccessDeniedError):
        await functions_rpc.delete_function_job_collection(
            rabbitmq_rpc_client=rpc_client,
            function_job_collection_id=registered_collection.uid,
            user_id=other_logged_user["id"],
            product_name=osparc_product_name,
        )

    await functions_rpc.delete_function_job_collection(
        rabbitmq_rpc_client=rpc_client,
        function_job_collection_id=registered_collection.uid,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    # Attempt to retrieve the deleted collection
    with pytest.raises(FunctionJobIDNotFoundError):
        await functions_rpc.get_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job_id=registered_collection.uid,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_function_job_collections(
    client: TestClient,
    mock_function: ProjectFunction,
    rpc_client: RabbitMQRPCClient,
    clean_functions: None,
    clean_function_job_collections: None,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # List function job collections when none are registered
    collections, page_meta = await functions_rpc.list_function_job_collections(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=0,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list is empty
    assert page_meta.count == 0
    assert page_meta.total == 0
    assert page_meta.offset == 0
    assert len(collections) == 0

    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    assert registered_function.uid is not None

    # Create a function job collection
    function_job_ids = []
    for _ in range(3):
        registered_function_job = ProjectFunctionJob(
            function_uid=registered_function.uid,
            title="Test Function Job",
            description="A test function job",
            project_job_id=uuid4(),
            inputs={"input1": "value1"},
            outputs={"output1": "result1"},
        )
        # Register the function job
        registered_job = await functions_rpc.register_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job=registered_function_job,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        assert registered_job.uid is not None
        function_job_ids.append(registered_job.uid)

    function_job_collection = FunctionJobCollection(
        title="Test Function Job Collection",
        description="A test function job collection",
        job_ids=function_job_ids,
    )

    # Register the function job collection
    registered_collections = [
        await functions_rpc.register_function_job_collection(
            rabbitmq_rpc_client=rpc_client,
            function_job_collection=function_job_collection,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        for _ in range(3)
    ]
    assert all(
        registered_collection.uid is not None
        for registered_collection in registered_collections
    )

    # List function job collections
    collections, page_params = await functions_rpc.list_function_job_collections(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=2,
        pagination_offset=1,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the registered collection
    assert page_params.count == 2
    assert page_params.total == 3
    assert page_params.offset == 1
    assert len(collections) == 2
    assert collections[0].uid in [
        collection.uid for collection in registered_collections
    ]
    assert collections[1].uid in [
        collection.uid for collection in registered_collections
    ]


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_list_function_job_collections_filtered_function_id(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    mock_function: ProjectFunction,
    clean_functions: None,
    clean_function_job_collections: None,
    logged_user: UserInfoDict,
    osparc_product_name: ProductName,
):
    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )
    other_registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    registered_collections = []
    for coll_i in range(5):
        if coll_i < 3:
            function_id = registered_function.uid
        else:
            function_id = other_registered_function.uid
        # Create a function job collection
        function_job_ids = []
        for _ in range(3):
            registered_function_job = ProjectFunctionJob(
                function_uid=function_id,
                title="Test Function Job",
                description="A test function job",
                project_job_id=uuid4(),
                inputs={"input1": "value1"},
                outputs={"output1": "result1"},
            )
            # Register the function job
            registered_job = await functions_rpc.register_function_job(
                rabbitmq_rpc_client=rpc_client,
                function_job=registered_function_job,
                user_id=logged_user["id"],
                product_name=osparc_product_name,
            )
            assert registered_job.uid is not None
            function_job_ids.append(registered_job.uid)

        function_job_collection = FunctionJobCollection(
            title="Test Function Job Collection",
            description="A test function job collection",
            job_ids=function_job_ids,
        )

        # Register the function job collection
        registered_collection = await functions_rpc.register_function_job_collection(
            rabbitmq_rpc_client=rpc_client,
            function_job_collection=function_job_collection,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        registered_collections.append(registered_collection)

    # List function job collections with a specific function ID
    collections, page_meta = await functions_rpc.list_function_job_collections(
        rabbitmq_rpc_client=rpc_client,
        pagination_limit=10,
        pagination_offset=1,
        filters=FunctionJobCollectionsListFilters(
            has_function_id=FunctionIDString(registered_function.uid)
        ),
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the list contains the registered collection
    assert page_meta.count == 2
    assert page_meta.total == 3
    assert page_meta.offset == 1

    assert len(collections) == 2
    assert collections[0].uid in [
        collection.uid for collection in registered_collections
    ]
    assert collections[1].uid in [
        collection.uid for collection in registered_collections
    ]


@pytest.mark.parametrize(
    "user_role",
    [UserRole.USER],
)
async def test_find_cached_function_jobs(
    client: TestClient,
    rpc_client: RabbitMQRPCClient,
    logged_user: UserInfoDict,
    other_logged_user: UserInfoDict,
    osparc_product_name: ProductName,
    mock_function: ProjectFunction,
    clean_functions: None,
):

    # Register the function first
    registered_function = await functions_rpc.register_function(
        rabbitmq_rpc_client=rpc_client,
        function=mock_function,
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    registered_function_jobs = []
    for value in range(5):
        function_job = ProjectFunctionJob(
            function_uid=registered_function.uid,
            title="Test Function Job",
            description="A test function job",
            project_job_id=uuid4(),
            inputs={"input1": value if value < 4 else 1},
            outputs={"output1": "result1"},
        )

        # Register the function job
        registered_job = await functions_rpc.register_function_job(
            rabbitmq_rpc_client=rpc_client,
            function_job=function_job,
            user_id=logged_user["id"],
            product_name=osparc_product_name,
        )
        registered_function_jobs.append(registered_job)

    # Find cached function jobs
    cached_jobs = await functions_rpc.find_cached_function_jobs(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        inputs={"input1": 1},
        user_id=logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the cached jobs contain the registered job
    assert cached_jobs is not None
    assert len(cached_jobs) == 2
    assert {job.uid for job in cached_jobs} == {
        registered_function_jobs[1].uid,
        registered_function_jobs[4].uid,
    }

    cached_jobs = await functions_rpc.find_cached_function_jobs(
        rabbitmq_rpc_client=rpc_client,
        function_id=registered_function.uid,
        inputs={"input1": 1},
        user_id=other_logged_user["id"],
        product_name=osparc_product_name,
    )

    # Assert the cached jobs does not contain the registered job for the other user
    assert cached_jobs is None
