"""Process-wide settings, read from environment variables (and `.env`).

Report-specific behaviour lives in `reports/*.yaml`; this module only holds what
is shared by every pipeline: infrastructure endpoints and runtime knobs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "default"
    clickhouse_password: SecretStr = SecretStr("")
    clickhouse_secure: bool = False
    clickhouse_metadata_database: str = "dashdashgo"
    clickhouse_connect_timeout_s: int = 10
    clickhouse_query_timeout_s: int = 300

    reports_dir: Path = Path("reports")
    # Reports shipped with the image; new ones are added to reports_dir at startup.
    bundled_reports_dir: Path | None = None
    storage_root: Path = Path("storage")
    storage_retention_days: int = Field(default=30, ge=0)

    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    scheduler_enabled: bool = True
    max_concurrent_runs: int = Field(default=2, ge=1, le=8)

    api_host: str = "0.0.0.0"
    api_port: int = 8000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
