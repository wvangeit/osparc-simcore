# pylint:disable=unused-variable
# pylint:disable=unused-argument
# pylint:disable=redefined-outer-name
# pylint:disable=protected-access
# pylint:disable=too-many-arguments
# pylint: disable=reimported
import asyncio
import functools
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, NoReturn, cast
from unittest import mock
from uuid import uuid4

import distributed
import pytest
import respx
from dask.distributed import get_worker
from dask_task_models_library.container_tasks.docker import DockerBasicAuth
from dask_task_models_library.container_tasks.errors import TaskCancelledError
from dask_task_models_library.container_tasks.events import (
    TaskProgressEvent,
)
from dask_task_models_library.container_tasks.io import (
    TaskCancelEventName,
    TaskInputData,
    TaskOutputData,
    TaskOutputDataSchema,
)
from dask_task_models_library.container_tasks.protocol import (
    ContainerEnvsDict,
    ContainerLabelsDict,
    ContainerTaskParameters,
    LogFileUploadURL,
)
from distributed import Event, Scheduler
from distributed.deploy.spec import SpecCluster
from faker import Faker
from fastapi.applications import FastAPI
from models_library.api_schemas_directorv2.services import NodeRequirements
from models_library.clusters import ClusterTypeInModel, NoAuthentication
from models_library.docker import to_simcore_runtime_docker_label_key
from models_library.projects import ProjectID
from models_library.projects_nodes_io import NodeID
from models_library.projects_state import RunningState
from models_library.resource_tracker import HardwareInfo
from models_library.services_types import ServiceRunID
from models_library.users import UserID
from pydantic import AnyUrl, ByteSize, TypeAdapter
from pytest_mock.plugin import MockerFixture
from pytest_simcore.helpers.logging_tools import log_context
from pytest_simcore.helpers.typing_env import EnvVarsDict
from settings_library.s3 import S3Settings
from simcore_sdk.node_ports_v2 import FileLinkType
from simcore_service_director_v2.core.errors import (
    ComputationalBackendNotConnectedError,
    ComputationalBackendTaskNotFoundError,
    ComputationalSchedulerChangedError,
    InsuficientComputationalResourcesError,
    MissingComputationalResourcesError,
)
from simcore_service_director_v2.models.comp_runs import RunMetadataDict
from simcore_service_director_v2.models.comp_tasks import Image
from simcore_service_director_v2.modules.dask_client import DaskClient, TaskHandlers
from tenacity.asyncio import AsyncRetrying
from tenacity.retry import retry_if_exception_type
from tenacity.stop import stop_after_delay
from tenacity.wait import wait_fixed, wait_random
from yarl import URL

_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS = 20


async def _assert_wait_for_cb_call(mocked_fct, timeout: int | None = None):
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(timeout or 10),
        wait=wait_random(0, 1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            print(
                f"waiting for call in mocked fct {mocked_fct}, "
                f"Attempt={attempt.retry_state.attempt_number}"
            )
            mocked_fct.assert_called_once()
            mocked_fct.assert_called_with()


async def _assert_wait_for_task_status(
    job_id: str,
    dask_client: DaskClient,
    expected_status: RunningState,
    timeout: int | None = None,  # noqa: ASYNC109
):
    async for attempt in AsyncRetrying(
        reraise=True,
        stop=stop_after_delay(timeout or _ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS),
        wait=wait_fixed(2),
        retry=retry_if_exception_type(AssertionError),
    ):
        with attempt:
            print(
                f"waiting for task to be {expected_status=}, "
                f"Attempt={attempt.retry_state.attempt_number}"
            )
            got = (await dask_client.get_tasks_status([job_id]))[0]
            assert isinstance(got, RunningState)
            print(f"{got=} vs {expected_status=}")
            if got is RunningState.FAILED and expected_status not in [
                RunningState.FAILED,
                RunningState.UNKNOWN,
            ]:
                try:
                    # we can fail fast here
                    # this will raise and we catch the Assertion to not reraise too long
                    await dask_client.get_task_result(job_id)
                except AssertionError as exc:
                    raise RuntimeError from exc
            assert got is expected_status


@pytest.fixture
def _minimal_dask_config(
    disable_postgres: None,
    mock_env: EnvVarsDict,
    project_env_devel_environment: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set a minimal configuration for testing the dask connection only"""
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("DIRECTOR_V2_DYNAMIC_SIDECAR_ENABLED", "false")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("DIRECTOR_V2_CATALOG", "null")
    monkeypatch.setenv("COMPUTATIONAL_BACKEND_DASK_CLIENT_ENABLED", "1")
    monkeypatch.setenv("COMPUTATIONAL_BACKEND_ENABLED", "0")
    monkeypatch.setenv("SC_BOOT_MODE", "production")


@pytest.fixture
async def create_dask_client_from_scheduler(
    _minimal_dask_config: None,
    dask_spec_local_cluster: distributed.SpecCluster,
    minimal_app: FastAPI,
    tasks_file_link_type: FileLinkType,
) -> AsyncIterator[Callable[[], Awaitable[DaskClient]]]:
    created_clients = []

    async def factory() -> DaskClient:
        with log_context(
            logging.INFO,
            f"Create director-v2 DaskClient to {dask_spec_local_cluster.scheduler_address}",
        ) as ctx:
            client = await DaskClient.create(
                app=minimal_app,
                settings=minimal_app.state.settings.DIRECTOR_V2_COMPUTATIONAL_BACKEND,
                endpoint=TypeAdapter(AnyUrl).validate_python(
                    dask_spec_local_cluster.scheduler_address
                ),
                authentication=NoAuthentication(),
                tasks_file_link_type=tasks_file_link_type,
                cluster_type=ClusterTypeInModel.ON_PREMISE,
            )
            assert client
            assert client.app == minimal_app
            assert (
                client.settings
                == minimal_app.state.settings.DIRECTOR_V2_COMPUTATIONAL_BACKEND
            )

            assert client.backend.client
            scheduler_infos = client.backend.client.scheduler_info()  # type: ignore
            ctx.logger.info(
                "%s",
                f"--> Connected to scheduler via client {client=} to scheduler {scheduler_infos=}",
            )

        created_clients.append(client)
        return client

    yield factory

    with log_context(logging.INFO, "Disconnect scheduler clients"):
        await asyncio.gather(*[client.delete() for client in created_clients])


@pytest.fixture(params=["create_dask_client_from_scheduler"])
async def dask_client(
    create_dask_client_from_scheduler: Callable[[], Awaitable[DaskClient]],
    request: pytest.FixtureRequest,
) -> DaskClient:
    client: DaskClient = await {
        "create_dask_client_from_scheduler": create_dask_client_from_scheduler,
    }[request.param]()

    try:
        assert client.app.state.engine is not None

        # check we can run some simple python script
        def _square(x):
            return x**2

        def neg(x):
            return -x

        a = client.backend.client.map(_square, range(10))
        b = client.backend.client.map(neg, a)
        total = client.backend.client.submit(sum, b)
        future = total.result()
        assert future
        assert isinstance(future, Coroutine)
        result = await future
        assert result == -285
    except AttributeError:
        # enforces existance of 'app.state.engine' and sets to None
        client.app.state.engine = None

    return client


@pytest.fixture
def project_id() -> ProjectID:
    return uuid4()


@dataclass
class ImageParams:
    image: Image
    expected_annotations: dict[str, Any]
    expected_used_resources: dict[str, Any]
    fake_tasks: dict[NodeID, Image]


@pytest.fixture
def cpu_image(node_id: NodeID) -> ImageParams:
    image = Image(
        name="simcore/services/comp/pytest/cpu_image",
        tag="1.5.5",
        node_requirements=NodeRequirements(
            CPU=1,
            RAM=TypeAdapter(ByteSize).validate_python("128 MiB"),
            GPU=None,
        ),
    )  # type: ignore
    return ImageParams(
        image=image,
        expected_annotations={
            "resources": {
                "CPU": 1.0,
                "RAM": 128 * 1024 * 1024,
            }
        },
        expected_used_resources={
            "CPU": 1.0,
            "RAM": 128 * 1024 * 1024.0,
        },
        fake_tasks={node_id: image},
    )


@pytest.fixture
def gpu_image(node_id: NodeID) -> ImageParams:
    image = Image(
        name="simcore/services/comp/pytest/gpu_image",
        tag="1.4.7",
        node_requirements=NodeRequirements(
            CPU=1,
            GPU=1,
            RAM=TypeAdapter(ByteSize).validate_python("256 MiB"),
        ),
    )  # type: ignore
    return ImageParams(
        image=image,
        expected_annotations={
            "resources": {
                "CPU": 1.0,
                "GPU": 1.0,
                "RAM": 256 * 1024 * 1024,
            },
        },
        expected_used_resources={
            "CPU": 1.0,
            "GPU": 1.0,
            "RAM": 256 * 1024 * 1024.0,
        },
        fake_tasks={node_id: image},
    )


@pytest.fixture(params=[cpu_image.__name__, gpu_image.__name__])
def image_params(
    cpu_image: ImageParams, gpu_image: ImageParams, request
) -> ImageParams:
    return {
        "cpu_image": cpu_image,
        "gpu_image": gpu_image,
    }[request.param]


@pytest.fixture
def _mocked_node_ports(mocker: MockerFixture) -> None:
    mocker.patch(
        "simcore_service_director_v2.modules.dask_client.dask_utils.create_node_ports",
        return_value=None,
    )

    mocker.patch(
        "simcore_service_director_v2.modules.dask_client.dask_utils.compute_input_data",
        return_value=TaskInputData.model_validate({}),
    )
    mocker.patch(
        "simcore_service_director_v2.modules.dask_client.dask_utils.compute_output_data_schema",
        return_value=TaskOutputDataSchema.model_validate({}),
    )
    mocker.patch(
        "simcore_service_director_v2.modules.dask_client.dask_utils.compute_service_log_file_upload_link",
        return_value=TypeAdapter(AnyUrl).validate_python("file://undefined"),
    )


@pytest.fixture
def mocked_user_completed_cb(mocker: MockerFixture) -> mock.MagicMock:
    return mocker.MagicMock()


async def test_dask_cluster_executes_simple_functions(dask_client: DaskClient):
    def test_fct_add(x: int, y: int) -> int:
        return x + y

    future = dask_client.backend.client.submit(test_fct_add, 2, 5)
    assert future

    result = await future.result(timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS)  # type: ignore
    assert result == 7


@pytest.mark.xfail(
    reason="BaseException is not propagated back by dask [https://github.com/dask/distributed/issues/5846]"
)
@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_dask_does_not_report_asyncio_cancelled_error_in_task(
    dask_client: DaskClient,
):
    def fct_that_raise_cancellation_error() -> NoReturn:
        import asyncio

        cancel_msg = "task was cancelled, but dask does not care..."
        raise asyncio.CancelledError(cancel_msg)

    future = dask_client.backend.client.submit(fct_that_raise_cancellation_error)
    # NOTE: Since asyncio.CancelledError is derived from BaseException and the worker code checks Exception only
    # this goes through...
    # The day this is fixed, this test should detect it... SAN would be happy to know about it.
    assert await future.exception(timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS)  # type: ignore
    assert future.cancelled() is True


@pytest.mark.xfail(
    reason="BaseException is not propagated back by dask [https://github.com/dask/distributed/issues/5846]"
)
@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_dask_does_not_report_base_exception_in_task(dask_client: DaskClient):
    def fct_that_raise_base_exception() -> NoReturn:
        err_msg = "task triggers a base exception, but dask does not care..."
        raise BaseException(  # pylint: disable=broad-exception-raised  # noqa: TRY002
            err_msg
        )

    future = dask_client.backend.client.submit(fct_that_raise_base_exception)
    # NOTE: Since asyncio.CancelledError is derived from BaseException and the worker code checks Exception only
    # this goes through...
    # The day this is fixed, this test should detect it... SAN would be happy to know about it.
    assert await future.exception(timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS)  # type: ignore
    assert future.cancelled() is True


@pytest.mark.parametrize("exc", [Exception, TaskCancelledError])
@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_dask_does_report_any_non_base_exception_derived_error(
    dask_client: DaskClient, exc: type[Exception]
):
    def fct_that_raise_exception():
        raise exc

    future = dask_client.backend.client.submit(fct_that_raise_exception)
    # NOTE: Since asyncio.CancelledError does not work we define our own Exception derived cancellation
    task_exception = await future.exception(
        timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )  # type: ignore
    assert task_exception
    assert isinstance(task_exception, exc)
    task_traceback = await future.traceback(
        timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )  # type: ignore
    assert task_traceback
    trace = traceback.format_exception(task_exception)
    assert trace


@pytest.fixture
def comp_run_metadata(faker: Faker) -> RunMetadataDict:
    return RunMetadataDict(
        product_name=faker.pystr(),
        simcore_user_agent=faker.pystr(),
    ) | cast(RunMetadataDict, faker.pydict(allowed_types=(str,)))


@pytest.fixture
def task_labels(comp_run_metadata: RunMetadataDict) -> ContainerLabelsDict:
    return TypeAdapter(ContainerLabelsDict).validate_python(
        {
            k.replace("_", "-").lower(): v
            for k, v in comp_run_metadata.items()
            if k not in ["product_name", "simcore_user_agent"]
        },
    )


@pytest.fixture
def hardware_info() -> HardwareInfo:
    assert "json_schema_extra" in HardwareInfo.model_config
    assert isinstance(HardwareInfo.model_config["json_schema_extra"], dict)
    assert isinstance(HardwareInfo.model_config["json_schema_extra"]["examples"], list)
    return HardwareInfo.model_validate(
        HardwareInfo.model_config["json_schema_extra"]["examples"][0]
    )


@pytest.fixture
def empty_hardware_info() -> HardwareInfo:
    return HardwareInfo(aws_ec2_instances=[])


async def test_send_computation_task(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    node_id: NodeID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    task_labels: ContainerLabelsDict,
    empty_hardware_info: HardwareInfo,
    faker: Faker,
    resource_tracking_run_id: ServiceRunID,
):
    _DASK_EVENT_NAME = faker.pystr()

    # NOTE: this must be inlined so that the test works,
    # the dask-worker must be able to import the function
    def fake_sidecar_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
        expected_annotations: dict[str, Any],
        expected_envs: ContainerEnvsDict,
        expected_labels: ContainerLabelsDict,
    ) -> TaskOutputData:
        # get the task data
        worker = get_worker()
        task = worker.state.tasks.get(worker.get_current_task())
        assert task is not None
        assert task.annotations == expected_annotations
        assert task_parameters.envs == expected_envs
        assert task_parameters.labels == expected_labels
        assert task_parameters.command == ["run"]
        event = distributed.Event(_DASK_EVENT_NAME)
        event.wait(timeout=25)

        return TaskOutputData.model_validate({"some_output_key": 123})

    # NOTE: We pass another fct so it can run in our localy created dask cluster
    # NOTE2: since there is only 1 task here, it's ok to pass the nodeID
    node_params = image_params.fake_tasks[node_id]
    assert node_params.node_requirements is not None
    assert node_params.node_requirements.cpu
    assert node_params.node_requirements.ram
    assert "product_name" in comp_run_metadata
    assert "simcore_user_agent" in comp_run_metadata
    node_requirements = image_params.fake_tasks[node_id].node_requirements
    assert node_requirements

    node_id_to_job_ids = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=image_params.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=functools.partial(
            fake_sidecar_fct,
            expected_annotations=image_params.expected_annotations,
            expected_envs={},
            expected_labels=task_labels
            | {
                f"{to_simcore_runtime_docker_label_key('user-id')}": f"{user_id}",
                f"{to_simcore_runtime_docker_label_key('project-id')}": f"{project_id}",
                f"{to_simcore_runtime_docker_label_key('node-id')}": f"{node_id}",
                f"{to_simcore_runtime_docker_label_key('cpu-limit')}": f"{node_requirements.cpu}",
                f"{to_simcore_runtime_docker_label_key('memory-limit')}": f"{node_requirements.ram}",
                f"{to_simcore_runtime_docker_label_key('product-name')}": f"{comp_run_metadata['product_name']}",
                f"{to_simcore_runtime_docker_label_key('simcore-user-agent')}": f"{comp_run_metadata['simcore_user_agent']}",
                f"{to_simcore_runtime_docker_label_key('swarm-stack-name')}": "undefined-label",
            },  # type: ignore
        ),
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert node_id_to_job_ids
    assert len(node_id_to_job_ids) == 1
    published_computation_task = node_id_to_job_ids[0]
    assert published_computation_task.node_id in image_params.fake_tasks

    # check status goes to PENDING/STARTED
    await _assert_wait_for_task_status(
        published_computation_task.job_id,
        dask_client,
        expected_status=RunningState.STARTED,
    )

    # using the event we let the remote fct continue
    event = distributed.Event(_DASK_EVENT_NAME, client=dask_client.backend.client)
    await event.set()  # type: ignore
    await _assert_wait_for_cb_call(
        mocked_user_completed_cb, timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )

    # check the task status
    await _assert_wait_for_task_status(
        published_computation_task.job_id,
        dask_client,
        expected_status=RunningState.SUCCESS,
    )

    # check the results
    task_result = await dask_client.get_task_result(published_computation_task.job_id)
    assert isinstance(task_result, TaskOutputData)
    assert task_result.get("some_output_key") == 123

    # now release the results
    await dask_client.release_task_result(published_computation_task.job_id)
    # check the status now
    await _assert_wait_for_task_status(
        published_computation_task.job_id,
        dask_client,
        expected_status=RunningState.UNKNOWN,
        timeout=60,
    )

    with pytest.raises(ComputationalBackendTaskNotFoundError):
        await dask_client.get_task_result(published_computation_task.job_id)


async def test_computation_task_is_persisted_on_dask_scheduler(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    """rationale:
    When a task is submitted to the dask backend, a dask future is returned.
    If the dask future goes out of scope, then the task is forgotten by the dask backend. So if
    for some reason the client gets deleted, or the director-v2, then all the futures would
    be deleted, thus stopping all the computations.
    To aleviate this, it is possible to persist the futures directly in the dask-scheduler.

    When submitting a computation task, the future corresponding to that task is "published" on the scheduler.
    """

    # NOTE: this must be inlined so that the test works,
    # the dask-worker must be able to import the function
    def fake_sidecar_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
    ) -> TaskOutputData:
        # get the task data
        worker = get_worker()
        task = worker.state.tasks.get(worker.get_current_task())
        assert task is not None

        return TaskOutputData.model_validate({"some_output_key": 123})

    # NOTE: We pass another fct so it can run in our localy created dask cluster
    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=image_params.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=fake_sidecar_fct,
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1
    await _assert_wait_for_cb_call(
        mocked_user_completed_cb, timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )
    # check the task status
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        expected_status=RunningState.SUCCESS,
    )
    assert published_computation_task[0].node_id in image_params.fake_tasks
    # creating a new future shows that it is not done????
    assert not distributed.Future(
        published_computation_task[0].job_id, client=dask_client.backend.client
    ).done()

    # as the task is published on the dask-scheduler when sending, it shall still be published on the dask scheduler
    list_of_persisted_datasets = await dask_client.backend.client.list_datasets()  # type: ignore
    assert list_of_persisted_datasets
    assert isinstance(list_of_persisted_datasets, tuple)
    assert len(list_of_persisted_datasets) == 1
    assert published_computation_task[0].job_id in list_of_persisted_datasets
    assert list_of_persisted_datasets[0] == published_computation_task[0].job_id
    # get the persisted future from the scheduler back
    task_future = await dask_client.backend.client.get_dataset(
        name=published_computation_task[0].job_id
    )  # type: ignore
    assert task_future
    assert isinstance(task_future, distributed.Future)
    assert task_future.key == published_computation_task[0].job_id
    # NOTE: the future was persisted BEFORE the computation was completed.. therefore it is not updated
    # this is a bit weird, but it is so, this assertion demonstrates it. we need to await the results.
    assert not task_future.done()
    task_result = await task_future.result(timeout=2)  # type: ignore
    # now the future is done
    assert task_future.done()
    assert isinstance(task_result, TaskOutputData)
    assert task_result.get("some_output_key") == 123
    # try to create another future and this one is already done
    assert distributed.Future(
        published_computation_task[0].job_id, client=dask_client.backend.client
    ).done()


async def test_abort_computation_tasks(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    faker: Faker,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    _DASK_EVENT_NAME = faker.pystr()

    # NOTE: this must be inlined so that the test works,
    # the dask-worker must be able to import the function
    def fake_remote_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
    ) -> TaskOutputData:
        # get the task data
        worker = get_worker()
        task = worker.state.tasks.get(worker.get_current_task())
        assert task is not None
        print(f"--> task {task=} started")
        cancel_event = Event(TaskCancelEventName.format(task.key))
        # tell the client we are started
        start_event = Event(_DASK_EVENT_NAME)
        start_event.set()
        # sleep a bit in case someone is aborting us
        print("--> waiting for task to be aborted...")
        cancel_event.wait(timeout=60)
        if cancel_event.is_set():
            # NOTE: asyncio.CancelledError is not propagated back to the client...
            print("--> raising cancellation error now")
            raise TaskCancelledError

        return TaskOutputData.model_validate({"some_output_key": 123})

    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=image_params.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=fake_remote_fct,
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1

    assert published_computation_task[0].node_id in image_params.fake_tasks
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        RunningState.STARTED,
    )

    # we wait to be sure the remote fct is started
    start_event = Event(_DASK_EVENT_NAME, client=dask_client.backend.client)
    await start_event.wait(timeout=10)  # type: ignore

    # now let's abort the computation
    cancel_event = await distributed.Event(
        name=TaskCancelEventName.format(published_computation_task[0].job_id),
        client=dask_client.backend.client,
    )
    await dask_client.abort_computation_task(published_computation_task[0].job_id)
    assert await cancel_event.is_set()  # type: ignore

    await _assert_wait_for_cb_call(mocked_user_completed_cb)
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id, dask_client, RunningState.ABORTED
    )

    # getting the results should throw the cancellation error
    with pytest.raises(TaskCancelledError):
        await dask_client.get_task_result(published_computation_task[0].job_id)

    await dask_client.release_task_result(published_computation_task[0].job_id)
    # after releasing the results, the task shall be UNKNOWN
    _ALLOW_TIME_FOR_LOCAL_DASK_SCHEDULER_TO_UPDATE_TIMEOUT_S = 5
    await asyncio.sleep(_ALLOW_TIME_FOR_LOCAL_DASK_SCHEDULER_TO_UPDATE_TIMEOUT_S)
    # NOTE: this change of status takes a very long time to happen and is not relied upon so we skip it since it
    # makes the test fail a lot for no gain (it's kept here in case it ever becomes an issue)
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        RunningState.UNKNOWN,
        timeout=10,
    )


async def test_failed_task_returns_exceptions(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    gpu_image: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # NOTE: this must be inlined so that the test works,
    # the dask-worker must be able to import the function
    def fake_failing_sidecar_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
    ) -> TaskOutputData:
        err_msg = "sadly we are failing to execute anything cause we are dumb..."
        raise ValueError(err_msg)

    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=gpu_image.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=fake_failing_sidecar_fct,
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1

    assert published_computation_task[0].node_id in gpu_image.fake_tasks

    # this waits for the computation to run
    await _assert_wait_for_cb_call(
        mocked_user_completed_cb, timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )

    # the computation status is FAILED
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        expected_status=RunningState.FAILED,
    )
    with pytest.raises(
        ValueError,
        match="sadly we are failing to execute anything cause we are dumb...",
    ):
        await dask_client.get_task_result(published_computation_task[0].job_id)
    assert len(await dask_client.backend.client.list_datasets()) > 0  # type: ignore
    await dask_client.release_task_result(published_computation_task[0].job_id)
    assert len(await dask_client.backend.client.list_datasets()) == 0  # type: ignore


# currently in the case of a dask-gateway we do not check for missing resources
@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_send_computation_task_with_missing_resources_raises(
    dask_spec_local_cluster: SpecCluster,
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # remove the workers that can handle gpu
    scheduler_info = dask_client.backend.client.scheduler_info()
    assert scheduler_info
    # find gpu workers
    workers_to_remove = [
        worker_key
        for worker_key, worker_info in scheduler_info["workers"].items()
        if "GPU" in worker_info["resources"]
    ]
    await dask_client.backend.client.retire_workers(workers=workers_to_remove)  # type: ignore
    await asyncio.sleep(5)  # a bit of time is needed so the cluster adapts

    # now let's adapt the task so it needs gpu
    assert image_params.image.node_requirements
    image_params.image.node_requirements.gpu = 2

    with pytest.raises(MissingComputationalResourcesError):
        await dask_client.send_computation_tasks(
            user_id=user_id,
            project_id=project_id,
            tasks=image_params.fake_tasks,
            callback=mocked_user_completed_cb,
            remote_fct=None,
            metadata=comp_run_metadata,
            hardware_info=empty_hardware_info,
            resource_tracking_run_id=resource_tracking_run_id,
        )
    mocked_user_completed_cb.assert_not_called()


@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_send_computation_task_with_hardware_info_raises(
    dask_spec_local_cluster: SpecCluster,
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # NOTE: running on the default cluster will raise missing resources
    with pytest.raises(MissingComputationalResourcesError):
        await dask_client.send_computation_tasks(
            user_id=user_id,
            project_id=project_id,
            tasks=image_params.fake_tasks,
            callback=mocked_user_completed_cb,
            remote_fct=None,
            metadata=comp_run_metadata,
            hardware_info=hardware_info,
            resource_tracking_run_id=resource_tracking_run_id,
        )
    mocked_user_completed_cb.assert_not_called()


@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_too_many_resources_send_computation_task(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    node_id: NodeID,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # create an image that needs a huge amount of CPU
    image = Image(
        name="simcore/services/comp/pytest",
        tag="1.4.5",
        node_requirements=NodeRequirements(
            CPU=10000000000000000,
            RAM=TypeAdapter(ByteSize).validate_python("128 MiB"),
            GPU=None,
        ),
    )  # type: ignore
    fake_task = {node_id: image}

    # let's have a big number of CPUs
    with pytest.raises(InsuficientComputationalResourcesError):
        await dask_client.send_computation_tasks(
            user_id=user_id,
            project_id=project_id,
            tasks=fake_task,
            callback=mocked_user_completed_cb,
            remote_fct=None,
            metadata=comp_run_metadata,
            hardware_info=empty_hardware_info,
            resource_tracking_run_id=resource_tracking_run_id,
        )

    mocked_user_completed_cb.assert_not_called()


async def test_disconnected_backend_raises_exception(
    dask_spec_local_cluster: SpecCluster,
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    cpu_image: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # DISCONNECT THE CLUSTER
    await dask_spec_local_cluster.close()  # type: ignore
    with pytest.raises(ComputationalBackendNotConnectedError):
        await dask_client.send_computation_tasks(
            user_id=user_id,
            project_id=project_id,
            tasks=cpu_image.fake_tasks,
            callback=mocked_user_completed_cb,
            remote_fct=None,
            metadata=comp_run_metadata,
            hardware_info=empty_hardware_info,
            resource_tracking_run_id=resource_tracking_run_id,
        )
    mocked_user_completed_cb.assert_not_called()


@pytest.mark.parametrize(
    "dask_client", ["create_dask_client_from_scheduler"], indirect=True
)
async def test_changed_scheduler_raises_exception(
    dask_spec_local_cluster: SpecCluster,
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    cpu_image: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    unused_tcp_port_factory: Callable,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # change the scheduler (stop the current one and start another at the same address)
    scheduler_address = URL(dask_spec_local_cluster.scheduler_address)
    await dask_spec_local_cluster.close()  # type: ignore

    scheduler = {
        "cls": Scheduler,
        "options": {
            "dashboard_address": f":{unused_tcp_port_factory()}",
            "port": scheduler_address.port,
        },
    }
    async with SpecCluster(
        scheduler=scheduler, asynchronous=True, name="pytest_cluster"
    ) as cluster:
        assert URL(cluster.scheduler_address) == scheduler_address

        # leave a bit of time to allow the client to reconnect automatically
        await asyncio.sleep(2)

        with pytest.raises(ComputationalSchedulerChangedError):
            await dask_client.send_computation_tasks(
                user_id=user_id,
                project_id=project_id,
                tasks=cpu_image.fake_tasks,
                callback=mocked_user_completed_cb,
                remote_fct=None,
                metadata=comp_run_metadata,
                hardware_info=empty_hardware_info,
                resource_tracking_run_id=resource_tracking_run_id,
            )
    mocked_user_completed_cb.assert_not_called()


@pytest.mark.parametrize("fail_remote_fct", [False, True])
async def test_get_tasks_status(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    cpu_image: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    faker: Faker,
    fail_remote_fct: bool,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    # NOTE: this must be inlined so that the test works,
    # the dask-worker must be able to import the function
    _DASK_EVENT_NAME = faker.pystr()

    def fake_remote_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
    ) -> TaskOutputData:
        # wait here until the client allows us to continue
        start_event = Event(_DASK_EVENT_NAME)
        start_event.wait(timeout=5)
        if fail_remote_fct:
            err_msg = "We fail because we're told to!"
            raise ValueError(err_msg)
        return TaskOutputData.model_validate({"some_output_key": 123})

    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=cpu_image.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=fake_remote_fct,
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1

    assert published_computation_task[0].node_id in cpu_image.fake_tasks

    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        RunningState.STARTED,
    )

    # let the remote fct run through now
    start_event = Event(_DASK_EVENT_NAME, dask_client.backend.client)
    await start_event.set()  # type: ignore
    # it will become successful hopefuly
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        RunningState.FAILED if fail_remote_fct else RunningState.SUCCESS,
    )
    # release the task results
    await dask_client.release_task_result(published_computation_task[0].job_id)

    await asyncio.sleep(
        5  # NOTE: here we wait to be sure that the dask-scheduler properly updates its state
    )
    # the task is gone, since the distributed Variable was removed above
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        RunningState.UNKNOWN,
        timeout=60,
    )


@pytest.fixture
async def fake_task_handlers(mocker: MockerFixture) -> TaskHandlers:
    return TaskHandlers(task_progress_handler=mocker.MagicMock())


async def test_dask_sub_handlers(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    cpu_image: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    fake_task_handlers: TaskHandlers,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    resource_tracking_run_id: ServiceRunID,
):
    dask_client.register_handlers(fake_task_handlers)
    _DASK_START_EVENT = "start"

    def fake_remote_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
    ) -> TaskOutputData:
        get_worker().log_event(TaskProgressEvent.topic_name(), "my name is progress")
        # tell the client we are done
        published_event = Event(name=_DASK_START_EVENT)
        published_event.set()

        return TaskOutputData.model_validate({"some_output_key": 123})

    # run the computation
    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=cpu_image.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=fake_remote_fct,
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1

    assert published_computation_task[0].node_id in cpu_image.fake_tasks
    computation_future = distributed.Future(
        published_computation_task[0].job_id, client=dask_client.backend.client
    )
    print("--> waiting for job to finish...")
    await distributed.wait(
        computation_future, timeout=_ALLOW_TIME_FOR_GATEWAY_TO_CREATE_WORKERS
    )
    assert computation_future.done()
    print("job finished, now checking that we received the publications...")

    async for attempt in AsyncRetrying(
        reraise=True,
        wait=wait_fixed(1),
        stop=stop_after_delay(5),
    ):
        with attempt:
            print(
                f"waiting for call in mocked fct {fake_task_handlers}, "
                f"Attempt={attempt.retry_state.attempt_number}"
            )
            # we should have received data in our TaskHandlers
            fake_task_handlers.task_progress_handler.assert_called_with(
                (mock.ANY, "my name is progress")
            )
    await _assert_wait_for_cb_call(mocked_user_completed_cb)


async def test_get_cluster_details(
    dask_client: DaskClient,
    user_id: UserID,
    project_id: ProjectID,
    image_params: ImageParams,
    _mocked_node_ports: None,
    mocked_user_completed_cb: mock.AsyncMock,
    mocked_storage_service_api: respx.MockRouter,
    comp_run_metadata: RunMetadataDict,
    empty_hardware_info: HardwareInfo,
    faker: Faker,
    resource_tracking_run_id: ServiceRunID,
):
    cluster_details = await dask_client.get_cluster_details()
    assert cluster_details

    _DASK_EVENT_NAME = faker.pystr()

    # send a fct that uses resources
    def fake_sidecar_fct(
        task_parameters: ContainerTaskParameters,
        docker_auth: DockerBasicAuth,
        log_file_url: LogFileUploadURL,
        s3_settings: S3Settings | None,
        expected_annotations,
    ) -> TaskOutputData:
        # get the task data
        worker = get_worker()
        task = worker.state.tasks.get(worker.get_current_task())
        assert task is not None
        assert task.annotations == expected_annotations
        assert task_parameters.command == ["run"]
        event = distributed.Event(_DASK_EVENT_NAME)
        event.wait(timeout=25)

        return TaskOutputData.model_validate({"some_output_key": 123})

    # NOTE: We pass another fct so it can run in our localy created dask cluster
    published_computation_task = await dask_client.send_computation_tasks(
        user_id=user_id,
        project_id=project_id,
        tasks=image_params.fake_tasks,
        callback=mocked_user_completed_cb,
        remote_fct=functools.partial(
            fake_sidecar_fct, expected_annotations=image_params.expected_annotations
        ),
        metadata=comp_run_metadata,
        hardware_info=empty_hardware_info,
        resource_tracking_run_id=resource_tracking_run_id,
    )
    assert published_computation_task
    assert len(published_computation_task) == 1

    assert published_computation_task[0].node_id in image_params.fake_tasks

    # check status goes to PENDING/STARTED
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        expected_status=RunningState.STARTED,
    )

    # check we have one worker using the resources
    # one of the workers should now get the job and use the resources
    worker_with_the_task: AnyUrl | None = None
    async for attempt in AsyncRetrying(reraise=True, stop=stop_after_delay(10)):
        with attempt:
            cluster_details = await dask_client.get_cluster_details()
            assert cluster_details
            assert (
                cluster_details.scheduler.workers
            ), f"there are no workers in {cluster_details.scheduler=!r}"
            for worker_url, worker_data in cluster_details.scheduler.workers.items():
                if all(
                    worker_data.used_resources.get(res_name) == res_value
                    for res_name, res_value in image_params.expected_used_resources.items()
                ):
                    worker_with_the_task = worker_url
            assert (
                worker_with_the_task is not None
            ), f"there is no worker in {cluster_details.scheduler.workers.keys()=} consuming {image_params.expected_annotations=!r}"

    # using the event we let the remote fct continue
    event = distributed.Event(_DASK_EVENT_NAME, client=dask_client.backend.client)
    await event.set()  # type: ignore

    # wait for the task to complete
    await _assert_wait_for_task_status(
        published_computation_task[0].job_id,
        dask_client,
        expected_status=RunningState.SUCCESS,
    )

    # check the resources are released
    cluster_details = await dask_client.get_cluster_details()
    assert cluster_details
    assert cluster_details.scheduler.workers
    assert worker_with_the_task
    currently_used_resources = cluster_details.scheduler.workers[
        worker_with_the_task
    ].used_resources

    assert all(res == 0.0 for res in currently_used_resources.values())
