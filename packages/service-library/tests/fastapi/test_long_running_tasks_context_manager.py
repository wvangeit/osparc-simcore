# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument

import asyncio
from typing import AsyncIterable, Final

import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, Depends, FastAPI, status
from httpx import AsyncClient
from pydantic import AnyHttpUrl, PositiveFloat, parse_obj_as
from servicelib.fastapi.long_running_tasks._context_manager import _ProgressManager
from servicelib.fastapi.long_running_tasks._errors import (
    TaskClientResultError,
    TaskClientTimeoutError,
)
from servicelib.fastapi.long_running_tasks.client import setup as setup_client
from servicelib.fastapi.long_running_tasks.client import periodic_task_result, Client
from servicelib.fastapi.long_running_tasks.server import (
    TaskId,
    TaskManager,
    TaskProgress,
    get_task_manager,
)
from servicelib.fastapi.long_running_tasks.server import setup as setup_server
from servicelib.fastapi.long_running_tasks.server import start_task

TASK_SLEEP_INTERVAL: Final[PositiveFloat] = 0.1

# UTILS


async def _assert_task_removed(
    async_client: AsyncClient, task_id: TaskId, router_prefix: str
) -> None:
    result = await async_client.get(f"{router_prefix}/tasks/{task_id}")
    assert result.status_code == status.HTTP_404_NOT_FOUND


# FIXTURES


async def a_test_task(task_progress: TaskProgress) -> int:
    task_progress.publish(message="starting", percent=0.0)
    await asyncio.sleep(TASK_SLEEP_INTERVAL)
    task_progress.publish(message="finished", percent=1.0)
    return 42


async def a_failing_test_task(task_progress: TaskProgress) -> None:
    task_progress.publish(message="starting", percent=0.0)
    await asyncio.sleep(TASK_SLEEP_INTERVAL)
    task_progress.publish(message="finished", percent=1.0)
    raise RuntimeError("I am failing as requested")


@pytest.fixture
def user_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/success", status_code=status.HTTP_200_OK)
    async def create_task_user_defined_route(
        task_manager: TaskManager = Depends(get_task_manager),
    ) -> TaskId:
        task_id = start_task(task_manager=task_manager, handler=a_test_task)
        return task_id

    @router.get("/api/failing", status_code=status.HTTP_200_OK)
    async def create_task_which_fails(
        task_manager: TaskManager = Depends(get_task_manager),
    ) -> TaskId:
        task_id = start_task(task_manager=task_manager, handler=a_failing_test_task)
        return task_id

    return router


@pytest.fixture
async def bg_task_app(
    user_routes: APIRouter, router_prefix: str
) -> AsyncIterable[FastAPI]:
    app = FastAPI()

    app.include_router(user_routes)

    setup_server(app, router_prefix=router_prefix)
    setup_client(app, router_prefix=router_prefix)

    async with LifespanManager(app):
        yield app


# TESTS


async def test_task_result(
    bg_task_app: FastAPI, async_client: AsyncClient, router_prefix: str
) -> None:
    result = await async_client.get("/api/success")
    assert result.status_code == status.HTTP_200_OK, result.text
    task_id = result.json()

    url = parse_obj_as(AnyHttpUrl, "http://backgroud.testserver.io")
    client = Client(app=bg_task_app, async_client=async_client, base_url=url)
    async with periodic_task_result(
        client,
        task_id,
        task_timeout=10,
        status_poll_interval=TASK_SLEEP_INTERVAL / 3,
    ) as result:
        assert result == 42

    await _assert_task_removed(async_client, task_id, router_prefix)


async def test_task_result_times_out(
    bg_task_app: FastAPI, async_client: AsyncClient, router_prefix: str
) -> None:
    result = await async_client.get("/api/success")
    assert result.status_code == status.HTTP_200_OK, result.text
    task_id = result.json()

    url = parse_obj_as(AnyHttpUrl, "http://backgroud.testserver.io")
    client = Client(app=bg_task_app, async_client=async_client, base_url=url)
    timeout = TASK_SLEEP_INTERVAL / 2
    with pytest.raises(TaskClientTimeoutError) as exec_info:
        async with periodic_task_result(
            client,
            task_id,
            task_timeout=timeout,
            status_poll_interval=TASK_SLEEP_INTERVAL / 3,
        ):
            pass
    assert (
        f"{exec_info.value}"
        == f"Timed out after {timeout} seconds while awaiting '{task_id}' to complete"
    )

    await _assert_task_removed(async_client, task_id, router_prefix)


async def test_task_result_task_result_is_an_error(
    bg_task_app: FastAPI, async_client: AsyncClient, router_prefix: str
) -> None:
    result = await async_client.get("/api/failing")
    assert result.status_code == status.HTTP_200_OK, result.text
    task_id = result.json()

    url = parse_obj_as(AnyHttpUrl, "http://backgroud.testserver.io")
    client = Client(app=bg_task_app, async_client=async_client, base_url=url)
    with pytest.raises(TaskClientResultError) as exec_info:
        async with periodic_task_result(
            client,
            task_id,
            task_timeout=10,
            status_poll_interval=TASK_SLEEP_INTERVAL / 3,
        ):
            pass
    assert f"{exec_info.value}".startswith(f"Task {task_id} finished with exception:")
    assert 'raise RuntimeError("I am failing as requested")' in f"{exec_info.value}"
    await _assert_task_removed(async_client, task_id, router_prefix)


@pytest.mark.parametrize("repeat", [1, 2, 10])
async def test_progress_updater(repeat: int) -> None:
    counter = 0
    received = ()

    async def progress_update(message: str, percent: float) -> None:
        nonlocal counter
        nonlocal received
        counter += 1
        received = (message, percent)

    progress_updater = _ProgressManager(progress_update)

    # different from None and the last value only
    # triggers once
    for _ in range(repeat):
        await progress_updater.update(message="")
        assert counter == 1
        assert received == ("", None)

    for _ in range(repeat):
        await progress_updater.update(percent=0.0)
        assert counter == 2
        assert received == ("", 0.0)

    for _ in range(repeat):
        await progress_updater.update(percent=1.0, message="done")
        assert counter == 3
        assert received == ("done", 1.0)

    # setting percent or message to None
    # will not trigger an event

    for _ in range(repeat):
        await progress_updater.update(message=None)
        assert counter == 3
        assert received == ("done", 1.0)

    for _ in range(repeat):
        await progress_updater.update(percent=None)
        assert counter == 3
        assert received == ("done", 1.0)

    for _ in range(repeat):
        await progress_updater.update(percent=None, message=None)
        assert counter == 3
        assert received == ("done", 1.0)

    for _ in range(repeat):
        await progress_updater.update()
        assert counter == 3
        assert received == ("done", 1.0)
