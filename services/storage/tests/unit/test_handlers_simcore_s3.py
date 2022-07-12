# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument
# pylint: disable=unused-variable
# pylint: disable=too-many-nested-blocks

import sys
from collections import deque
from copy import deepcopy
from pathlib import Path
from random import randint
from secrets import choice
from typing import Any, Awaitable, Callable, Optional

import pytest
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import TestClient
from aiopg.sa.engine import Engine
from faker import Faker
from models_library.api_schemas_storage import FileMetaDataGet, FoldersBody
from models_library.projects import Project, ProjectID
from models_library.projects_nodes_io import NodeID, NodeIDStr, SimcoreS3FileID
from models_library.users import UserID
from models_library.utils.change_case import camel_to_snake
from models_library.utils.fastapi_encoders import jsonable_encoder
from pydantic import ByteSize, parse_file_as, parse_obj_as
from pytest_mock import MockerFixture
from pytest_simcore.helpers.utils_assert import assert_status
from servicelib.utils import logged_gather
from settings_library.s3 import S3Settings
from simcore_postgres_database.storage_models import file_meta_data, projects
from simcore_service_storage.s3_client import StorageS3Client
from simcore_service_storage.simcore_s3_dsm import SimcoreS3DataManager
from tests.helpers.utils_file_meta_data import assert_file_meta_data_in_db
from tests.helpers.utils_project import clone_project_data
from yarl import URL

pytest_simcore_core_services_selection = ["postgres"]
pytest_simcore_ops_services_selection = ["adminer"]


@pytest.fixture
def mock_datcore_download(mocker, client):
    # Use to mock downloading from DATCore
    async def _fake_download_to_file_or_raise(session, url, dest_path):
        print(f"Faking download:  {url} -> {dest_path}")
        Path(dest_path).write_text("FAKE: test_create_and_delete_folders_from_project")

    mocker.patch(
        "simcore_service_storage.simcore_s3_dsm.download_to_file_or_raise",
        side_effect=_fake_download_to_file_or_raise,
        autospec=True,
    )

    mocker.patch(
        "simcore_service_storage.simcore_s3_dsm.datcore_adapter.get_file_download_presigned_link",
        autospec=True,
        return_value=URL("https://httpbin.org/image"),
    )


async def test_simcore_s3_access_returns_default(client: TestClient):
    assert client.app
    url = (
        client.app.router["get_or_create_temporary_s3_access"]
        .url_for()
        .with_query(user_id=1)
    )
    response = await client.post(f"{url}")
    data, error = await assert_status(response, web.HTTPOk)
    assert not error
    assert data
    received_settings = S3Settings.parse_obj(data)
    assert received_settings


async def test_copy_folders_from_non_existing_project(
    client: TestClient,
    user_id: UserID,
    create_project: Callable[[], Awaitable[dict[str, Any]]],
    faker: Faker,
):
    assert client.app
    url = (
        client.app.router["copy_folders_from_project"]
        .url_for()
        .with_query(user_id=user_id)
    )
    src_project = await create_project()
    incorrect_src_project = deepcopy(src_project)
    incorrect_src_project["uuid"] = faker.uuid4()
    dst_project = await create_project()
    incorrect_dst_project = deepcopy(dst_project)
    incorrect_dst_project["uuid"] = faker.uuid4()

    response = await client.post(
        f"{url}",
        json=jsonable_encoder(
            FoldersBody(
                source=incorrect_src_project, destination=dst_project, nodes_map={}
            )
        ),
    )
    data, error = await assert_status(response, web.HTTPNotFound)
    assert error
    assert not data

    response = await client.post(
        f"{url}",
        json=jsonable_encoder(
            FoldersBody(
                source=src_project, destination=incorrect_dst_project, nodes_map={}
            )
        ),
    )
    data, error = await assert_status(response, web.HTTPNotFound)
    assert error
    assert not data


async def test_copy_folders_from_empty_project(
    client: TestClient,
    user_id: UserID,
    create_project: Callable[[], Awaitable[dict[str, Any]]],
    aiopg_engine: Engine,
    storage_s3_client: StorageS3Client,
):
    assert client.app
    url = (
        client.app.router["copy_folders_from_project"]
        .url_for()
        .with_query(user_id=user_id)
    )

    # we will copy from src to dst
    src_project = await create_project()
    dst_project = await create_project()

    response = await client.post(
        f"{url}",
        json=jsonable_encoder(
            FoldersBody(source=src_project, destination=dst_project, nodes_map={})
        ),
    )
    data, error = await assert_status(response, web.HTTPCreated)
    assert not error
    assert data == jsonable_encoder(dst_project)
    # check there is nothing in the dst project
    async with aiopg_engine.acquire() as conn:
        num_entries = await conn.scalar(
            sa.select([sa.func.count()])
            .select_from(file_meta_data)
            .where(file_meta_data.c.project_id == dst_project["uuid"])
        )
        assert num_entries == 0


async def _get_updated_project(aiopg_engine: Engine, project_id: str) -> dict[str, Any]:
    async with aiopg_engine.acquire() as conn:
        result = await conn.execute(
            sa.select([projects]).where(projects.c.uuid == project_id)
        )
        row = await result.fetchone()
        assert row
        return dict(row)


@pytest.fixture
async def random_project_with_files(
    aiopg_engine: Engine,
    create_project: Callable[[], Awaitable[dict[str, Any]]],
    create_project_node: Callable[[ProjectID], Awaitable[NodeID]],
    create_simcore_file_id: Callable[[ProjectID, NodeID, str], SimcoreS3FileID],
    upload_file: Callable[
        [ByteSize, str, str], Awaitable[tuple[Path, SimcoreS3FileID]]
    ],
    faker: Faker,
) -> tuple[dict[str, Any], dict[NodeID, dict[SimcoreS3FileID, Path]]]:
    project = await create_project()
    NUM_NODES = 12
    FILE_SIZES = [
        parse_obj_as(ByteSize, "7Mib"),
        parse_obj_as(ByteSize, "110Mib"),
        parse_obj_as(ByteSize, "1Mib"),
    ]
    src_projects_list: dict[NodeID, dict[SimcoreS3FileID, Path]] = {}
    upload_tasks: deque[Awaitable] = deque()
    for _node_index in range(NUM_NODES):
        src_node_id = await create_project_node(ProjectID(project["uuid"]))
        src_projects_list[src_node_id] = {}

        async def _upload_file_and_update_project(project, src_node_id):
            src_file_name = faker.file_name()
            src_file_uuid = create_simcore_file_id(
                ProjectID(project["uuid"]), src_node_id, src_file_name
            )
            src_file, _ = await upload_file(
                choice(FILE_SIZES), src_file_name, src_file_uuid
            )
            src_projects_list[src_node_id][src_file_uuid] = src_file

        upload_tasks.extend(
            [
                _upload_file_and_update_project(project, src_node_id)
                for _ in range(randint(0, 3))
            ]
        )
    await logged_gather(*upload_tasks, max_concurrency=2)

    project = await _get_updated_project(aiopg_engine, project["uuid"])
    return project, src_projects_list


async def test_copy_folders_from_valid_project(
    client: TestClient,
    user_id: UserID,
    create_project: Callable[[], Awaitable[dict[str, Any]]],
    create_simcore_file_id: Callable[[ProjectID, NodeID, str], SimcoreS3FileID],
    aiopg_engine: Engine,
    random_project_with_files: tuple[
        dict[str, Any], dict[NodeID, dict[SimcoreS3FileID, Path]]
    ],
):
    assert client.app
    url = (
        client.app.router["copy_folders_from_project"]
        .url_for()
        .with_query(user_id=user_id)
    )

    # 1. create a src project with some files
    src_project, src_projects_list = random_project_with_files
    # 2. create a dst project without files
    dst_project, nodes_map = clone_project_data(src_project)
    dst_project = await create_project(**dst_project)
    # copy the project files
    response = await client.post(
        f"{url}",
        json=jsonable_encoder(
            FoldersBody(
                source=src_project,
                destination=dst_project,
                nodes_map={NodeID(i): NodeID(j) for i, j in nodes_map.items()},
            )
        ),
    )
    data, error = await assert_status(response, web.HTTPCreated)
    assert not error
    assert data == jsonable_encoder(
        await _get_updated_project(aiopg_engine, dst_project["uuid"])
    )
    # check that file meta data was effectively copied
    for src_node_id in src_projects_list:
        dst_node_id = nodes_map.get(NodeIDStr(f"{src_node_id}"))
        assert dst_node_id
        for src_file in src_projects_list[src_node_id].values():
            await assert_file_meta_data_in_db(
                aiopg_engine,
                file_id=create_simcore_file_id(
                    ProjectID(dst_project["uuid"]), NodeID(dst_node_id), src_file.name
                ),
                expected_entry_exists=True,
                expected_file_size=src_file.stat().st_size,
                expected_upload_id=None,
                expected_upload_expiration_date=None,
            )


current_dir = Path(sys.argv[0] if __name__ == "__main__" else __file__).resolve().parent


def _get_project_with_data() -> list[Project]:
    projects = parse_file_as(
        list[Project], current_dir / "../data/projects_with_data.json"
    )
    assert projects
    return projects


async def _create_and_delete_folders_from_project(
    user_id: UserID,
    project: dict[str, Any],
    client: TestClient,
    project_db_creator: Callable,
    check_list_files: bool,
):
    destination_project, nodes_map = clone_project_data(project)
    await project_db_creator(**destination_project)

    # creating a copy
    assert client.app
    url = (
        client.app.router["copy_folders_from_project"]
        .url_for()
        .with_query(user_id=f"{user_id}")
    )
    resp = await client.post(
        f"{url}",
        json=jsonable_encoder(
            FoldersBody(
                source=project,
                destination=destination_project,
                nodes_map={NodeID(i): NodeID(j) for i, j in nodes_map.items()},
            )
        ),
    )

    data, _error = await assert_status(resp, expected_cls=web.HTTPCreated)

    # data should be equal to the destination project, and all store entries should point to simcore.s3
    for key in data:
        if key != "workbench":
            assert data[key] == destination_project[key]
        else:
            for _node_id, node in data[key].items():
                if "outputs" in node:
                    for _o_id, o in node["outputs"].items():
                        if "store" in o:
                            assert o["store"] == SimcoreS3DataManager.get_location_id()
    project_id = data["uuid"]

    # list data to check all is here
    if check_list_files:
        url = (
            client.app.router["get_files_metadata"]
            .url_for(location_id=f"{SimcoreS3DataManager.get_location_id()}")
            .with_query(user_id=f"{user_id}", uuid_filter=f"{project_id}")
        )
        resp = await client.get(f"{url}")
        data, error = await assert_status(resp, web.HTTPOk)
        assert not error
    # DELETING
    url = (
        client.app.router["delete_folders_of_project"]
        .url_for(folder_id=project_id)
        .with_query(user_id=f"{user_id}")
    )
    resp = await client.delete(f"{url}")

    await assert_status(resp, expected_cls=web.HTTPNoContent)

    # list data is gone
    if check_list_files:
        url = (
            client.app.router["get_files_metadata"]
            .url_for(location_id=f"{SimcoreS3DataManager.get_location_id()}")
            .with_query(user_id=f"{user_id}", uuid_filter=f"{project_id}")
        )
        resp = await client.get(f"{url}")
        data, error = await assert_status(resp, web.HTTPOk)
        assert not error
        assert not data


@pytest.fixture
def mock_check_project_exists(mocker: MockerFixture):
    # NOTE: this avoid having to inject project in database
    mock = mocker.patch(
        "simcore_service_storage.dsm._check_project_exists",
        autospec=True,
        return_value=None,
    )


@pytest.mark.flaky(max_runs=3)
@pytest.mark.parametrize(
    "project",
    [pytest.param(prj, id=prj.name) for prj in _get_project_with_data()],
)
async def test_create_and_delete_folders_from_project(
    client: TestClient,
    user_id: UserID,
    project: Project,
    create_project: Callable[..., Awaitable[dict[str, Any]]],
    mock_datcore_download,
):
    project_as_dict = jsonable_encoder(project, exclude={"tags", "state", "prj_owner"})
    # HACK: some key names must be changed but not all
    KEYS = {"creationDate", "lastChangeDate", "accessRights"}
    for k in KEYS:
        project_as_dict[camel_to_snake(k)] = project_as_dict.pop(k, None)

    await create_project(**project_as_dict)
    await _create_and_delete_folders_from_project(
        user_id, project_as_dict, client, create_project, check_list_files=True
    )


@pytest.mark.parametrize(
    "project",
    [pytest.param(prj, id=prj.name) for prj in _get_project_with_data()],
)
async def test_create_and_delete_folders_from_project_burst(
    client: TestClient,
    user_id: UserID,
    project: Project,
    create_project: Callable[..., Awaitable[dict[str, Any]]],
    mock_datcore_download,
):
    project_as_dict = jsonable_encoder(
        project, exclude={"tags", "state", "prj_owner"}, by_alias=False
    )
    await create_project(**project_as_dict)
    await logged_gather(
        *[
            _create_and_delete_folders_from_project(
                user_id, project_as_dict, client, create_project, check_list_files=False
            )
            for _ in range(100)
        ],
        max_concurrency=2,
    )


async def test_search_files_starting_with(
    client: TestClient,
    user_id: UserID,
    upload_file: Callable[
        [ByteSize, str, Optional[str]], Awaitable[tuple[Path, SimcoreS3FileID]]
    ],
    faker: Faker,
):
    assert client.app
    url = (
        client.app.router["search_files_starting_with"]
        .url_for()
        .with_query(user_id=user_id, startswith="")
    )

    response = await client.post(f"{url}")
    data, error = await assert_status(response, web.HTTPOk)
    assert not error
    list_fmds = parse_obj_as(list[FileMetaDataGet], data)
    assert not list_fmds

    # let's upload some files now
    file, file_id = await upload_file(
        parse_obj_as(ByteSize, "10Mib"), faker.file_name(), None
    )
    # search again should return something
    response = await client.post(f"{url}")
    data, error = await assert_status(response, web.HTTPOk)
    assert not error
    list_fmds = parse_obj_as(list[FileMetaDataGet], data)
    assert len(list_fmds) == 1
    assert list_fmds[0].file_id == file_id
    assert list_fmds[0].file_size == file.stat().st_size
    # search again with part of the file uuid shall return the same
    url.update_query(startswith=file_id[0:5])
    response = await client.post(f"{url}")
    data, error = await assert_status(response, web.HTTPOk)
    assert not error
    list_fmds = parse_obj_as(list[FileMetaDataGet], data)
    assert len(list_fmds) == 1
    assert list_fmds[0].file_id == file_id
    assert list_fmds[0].file_size == file.stat().st_size
    # search again with some other stuff shall return empty
    url = url.update_query(startswith="Iamlookingforsomethingthatdoesnotexist")
    response = await client.post(f"{url}")
    data, error = await assert_status(response, web.HTTPOk)
    assert not error
    list_fmds = parse_obj_as(list[FileMetaDataGet], data)
    assert not list_fmds