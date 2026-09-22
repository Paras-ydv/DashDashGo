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
    # Empty values (e.g. `AI_MODEL:` passed through by Compose) mean "use the default".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

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
    # HTTP Basic auth for the UI and API; disabled while either is empty.
    auth_username: str = ""
    auth_password: SecretStr = SecretStr("")

    # Optional AI assistant (failure diagnosis, config drafting) through any
    # OpenAI-compatible chat API. Disabled while ai_api_key is empty.
    ai_api_key: SecretStr = SecretStr("")
    ai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Comma list, tried in order: later models are fallbacks when one is overloaded.
    ai_model: str = "gemini-3.6-flash,gemini-flash-latest"
    ai_timeout_s: float = Field(default=60, gt=0, le=600)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password.get_secret_value())

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
