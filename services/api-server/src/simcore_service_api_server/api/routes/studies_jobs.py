import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse
from pydantic.types import PositiveInt

from ...models.pagination import Page, PaginationParams
from ...models.schemas.errors import ErrorGet
from ...models.schemas.jobs import (
    Job,
    JobID,
    JobInputs,
    JobMetadata,
    JobMetadataUpdate,
    JobOutputs,
    JobStatus,
)
from ...models.schemas.studies import Study, StudyID
from ...services.director_v2 import DirectorV2Api
from ...services.solver_job_models_converters import create_jobstatus_from_task
from ...services.study_job_models_converters import (
    create_job_from_study,
    create_job_outputs_from_project_outputs,
    get_project_and_file_inputs_from_job_inputs,
)
from ...services.webserver import AuthSession, ProjectNotFoundError
from ..dependencies.authentication import get_current_user_id
from ..dependencies.services import get_api_client
from ..dependencies.webserver import get_webserver_session
from ..errors.http_error import create_error_json_response
from ._common import API_SERVER_DEV_FEATURES_ENABLED, job_output_logfile_responses

_logger = logging.getLogger(__name__)
router = APIRouter()


def _compose_job_resource_name(study_key, job_id) -> str:
    """Creates a unique resource name for solver's jobs"""
    return Job.compose_resource_name(
        parent_name=Study.compose_resource_name(study_key),  # type: ignore
        job_id=job_id,
    )


@router.get(
    "/{study_id:uuid}/jobs",
    response_model=Page[Job],
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def list_study_jobs(
    study_id: StudyID,
    page_params: Annotated[PaginationParams, Depends()],
):
    msg = (
        f"list study jobs study_id={study_id!r} with "
        f"pagination={page_params!r}. "
        "SEE https://github.com/ITISFoundation/osparc-simcore/issues/4177"
    )
    raise NotImplementedError(msg)


@router.post(
    "/{study_id:uuid}/jobs",
    response_model=Job,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def create_study_job(
    study_id: StudyID,
    job_inputs: JobInputs,
    webserver_api: Annotated[AuthSession, Depends(get_webserver_session)],
):
    try:
        project = await webserver_api.clone_project(project_id=study_id)
        project_inputs = await webserver_api.get_project_inputs(project_id=project.uuid)

        file_param_nodes = {}
        for node_id, node in project.workbench.items():
            if (
                node.key == "simcore/services/frontend/file-picker"
                and len(node.outputs) == 0
            ):
                file_param_nodes[node.label] = node_id

        file_inputs = {}

        (
            new_project_inputs,
            new_project_file_inputs,
        ) = get_project_and_file_inputs_from_job_inputs(
            project_inputs, file_inputs, job_inputs
        )

        for node_label, file_link in new_project_file_inputs.items():
            node_id = file_param_nodes[node_label]

            await webserver_api.update_node_outputs(
                project.uuid, node_id, {"outputs": {"outFile": file_link}}
            )

        if len(new_project_inputs) > 0:
            await webserver_api.update_project_inputs(project.uuid, new_project_inputs)

        job = create_job_from_study(
            study_key=study_id, project=project, job_inputs=job_inputs
        )

        assert job.name == _compose_job_resource_name(study_id, job.id)

        return job

    except ProjectNotFoundError:
        return create_error_json_response(
            f"Cannot find study={study_id!r}.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


@router.get(
    "/{study_id:uuid}/jobs/{job_id:uuid}",
    response_model=Job,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def get_study_job(
    study_id: StudyID,
    job_id: JobID,
):
    msg = f"get study job study_id={study_id!r} job_id={job_id!r}. SEE https://github.com/ITISFoundation/osparc-simcore/issues/4177"
    raise NotImplementedError(msg)


@router.delete(
    "/{study_id:uuid}/jobs/{job_id:uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorGet}},
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def delete_study_job(
    study_id: StudyID,
    job_id: JobID,
    webserver_api: Annotated[AuthSession, Depends(get_webserver_session)],
):
    """Deletes an existing study job"""
    job_name = _compose_job_resource_name(study_id, job_id)
    _logger.debug("Deleting Job '%s'", job_name)

    try:
        await webserver_api.delete_project(project_id=job_id)
    except ProjectNotFoundError:
        return create_error_json_response(
            f"Cannot find job={job_id} to delete",
            status_code=status.HTTP_404_NOT_FOUND,
        )


@router.post(
    "/{study_id:uuid}/jobs/{job_id:uuid}:start",
    response_model=JobStatus,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def start_study_job(
    study_id: StudyID,
    job_id: JobID,
    user_id: Annotated[PositiveInt, Depends(get_current_user_id)],
    webserver_api: Annotated[AuthSession, Depends(get_webserver_session)],
    director2_api: Annotated[DirectorV2Api, Depends(get_api_client(DirectorV2Api))],
):
    job_name = _compose_job_resource_name(study_id, job_id)
    _logger.debug("Starting Job '%s'", job_name)

    await webserver_api.start_project(project_id=job_id)

    return await inspect_study_job(
        study_id=study_id,
        job_id=job_id,
        user_id=user_id,
        director2_api=director2_api,
    )


@router.post(
    "/{study_id:uuid}/jobs/{job_id:uuid}:stop",
    response_model=JobStatus,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def stop_study_job(
    study_id: StudyID,
    job_id: JobID,
    user_id: Annotated[PositiveInt, Depends(get_current_user_id)],
    director2_api: Annotated[DirectorV2Api, Depends(get_api_client(DirectorV2Api))],
):
    job_name = _compose_job_resource_name(study_id, job_id)
    _logger.debug("Stopping Job '%s'", job_name)

    await director2_api.stop_computation(job_id, user_id)

    task = await director2_api.get_computation(job_id, user_id)
    job_status: JobStatus = create_jobstatus_from_task(task)
    return job_status


@router.post(
    "/{study_id}/jobs/{job_id}:inspect",
    response_model=JobStatus,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def inspect_study_job(
    study_id: StudyID,
    job_id: JobID,
    user_id: Annotated[PositiveInt, Depends(get_current_user_id)],
    director2_api: Annotated[DirectorV2Api, Depends(get_api_client(DirectorV2Api))],
):
    job_name = _compose_job_resource_name(study_id, job_id)
    _logger.debug("Inspecting Job '%s'", job_name)

    task = await director2_api.get_computation(job_id, user_id)
    job_status: JobStatus = create_jobstatus_from_task(task)
    return job_status


@router.post(
    "/{study_id}/jobs/{job_id}/outputs",
    response_model=JobOutputs,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def get_study_job_outputs(
    study_id: StudyID,
    job_id: JobID,
    webserver_api: Annotated[AuthSession, Depends(get_webserver_session)],
):
    job_name = _compose_job_resource_name(study_id, job_id)
    _logger.debug("Getting Job Outputs for '%s'", job_name)

    project_outputs = await webserver_api.get_project_outputs(job_id)
    job_outputs = create_job_outputs_from_project_outputs(job_id, project_outputs)

    return job_outputs


@router.post(
    "/{study_id}/jobs/{job_id}/outputs/logfile",
    response_class=RedirectResponse,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
    responses=job_output_logfile_responses,
)
async def get_study_job_output_logfile(study_id: StudyID, job_id: JobID):
    msg = f"get study job output logfile study_id={study_id!r} job_id={job_id!r}. SEE https://github.com/ITISFoundation/osparc-simcore/issues/4177"
    raise NotImplementedError(msg)


@router.get(
    "/{study_id}/jobs/{job_id}/metadata",
    response_model=JobMetadata,
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def get_study_job_custom_metadata(
    study_id: StudyID,
    job_id: JobID,
):
    """Gets custom metadata from a job"""
    msg = f"Gets metadata attached to study_id={study_id!r} job_id={job_id!r}. SEE https://github.com/ITISFoundation/osparc-simcore/issues/4313"
    raise NotImplementedError(msg)


@router.put(
    "/{study_id}/jobs/{job_id}/metadata",
    include_in_schema=API_SERVER_DEV_FEATURES_ENABLED,
)
async def replace_study_job_custom_metadata(
    study_id: StudyID, job_id: JobID, replace: JobMetadataUpdate
):
    """Changes job's custom metadata"""
    msg = f"Attaches metadata={replace.metadata!r} to study_id={study_id!r} job_id={job_id!r}. SEE https://github.com/ITISFoundation/osparc-simcore/issues/4313"
    raise NotImplementedError(msg)
