from typing import Final

from models_library.basic_types import BootModeEnum, LogLevel
from pydantic import Field, NonNegativeInt, validator
from settings_library.base import BaseCustomSettings
from settings_library.r_clone import S3Provider
from settings_library.utils_logging import MixinLoggingSettings

_MINUTE: Final[NonNegativeInt] = 60


class ApplicationSettings(BaseCustomSettings, MixinLoggingSettings):
    LOGLEVEL: LogLevel = Field(
        LogLevel.WARNING.value, env=["WEBSERVER_LOGLEVEL", "LOG_LEVEL", "LOGLEVEL"]
    )
    SC_BOOT_MODE: BootModeEnum | None

    AGENT_VOLUMES_LOG_FORMAT_LOCAL_DEV_ENABLED: bool = Field(
        False,
        env=[
            "AGENT_VOLUMES_LOG_FORMAT_LOCAL_DEV_ENABLED",
            "LOG_FORMAT_LOCAL_DEV_ENABLED",
        ],
        description="Enables local development log format. WARNING: make sure it is disabled if you want to have structured logs!",
    )
    AGENT_VOLUMES_CLEANUP_TARGET_SWARM_STACK_NAME: str = Field(
        ..., description="Exactly the same as director-v2's `SWARM_STACK_NAME` env var"
    )
    AGENT_VOLUMES_CLEANUP_S3_SECURE: bool = False
    AGENT_VOLUMES_CLEANUP_S3_ENDPOINT: str
    AGENT_VOLUMES_CLEANUP_S3_ACCESS_KEY: str
    AGENT_VOLUMES_CLEANUP_S3_SECRET_KEY: str
    AGENT_VOLUMES_CLEANUP_S3_BUCKET: str
    AGENT_VOLUMES_CLEANUP_S3_PROVIDER: S3Provider
    AGENT_VOLUMES_CLEANUP_S3_REGION: str = "us-east-1"
    AGENT_VOLUMES_CLEANUP_RETRIES: int = Field(
        3, description="upload retries in case of error"
    )
    AGENT_VOLUMES_CLEANUP_PARALLELISM: int = Field(
        5, description="parallel transfers to s3"
    )
    AGENT_VOLUMES_CLEANUP_EXCLUDE_FILES: list[str] = Field(
        [".hidden_do_not_remove", "key_values.json"],
        description="Files to ignore when syncing to s3",
    )
    AGENT_VOLUMES_CLEANUP_INTERVAL_S: NonNegativeInt = Field(
        60 * _MINUTE, description="interval at which to repeat volumes cleanup"
    )

    @validator("AGENT_VOLUMES_CLEANUP_S3_ENDPOINT", pre=True)
    @classmethod
    def ensure_scheme(cls, v: str, values) -> str:
        if not v.startswith("http"):
            scheme = (
                "https" if values.get("AGENT_VOLUMES_CLEANUP_S3_SECURE") else "http"
            )
            return f"{scheme}://{v}"
        return v

    @validator("LOGLEVEL")
    @classmethod
    def valid_log_level(cls, value) -> LogLevel:
        return LogLevel(cls.validate_log_level(value))
