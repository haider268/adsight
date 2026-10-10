"""Runtime settings, read from ADSIGHT_* environment variables and an optional .env file.

Provider keys use their vendors' usual names (GROQ_API_KEY, GOOGLE_API_KEY,
ANTHROPIC_API_KEY), are held as secrets and are never logged.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ADSIGHT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    data_dir: Path = Path("data")
    seed: int = 7
    log_level: str = "INFO"
    state_dir: Path = Path(".adsight")
    anthropic_budget_usd: float = 0.0  # 0 means no Anthropic call is allowed

    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    google_api_key: SecretStr | None = Field(default=None, validation_alias="GOOGLE_API_KEY")
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "adsight.duckdb"

    @property
    def llm_cache_path(self) -> Path:
        return self.state_dir / "llm_cache.sqlite"

    @property
    def runs_db_path(self) -> Path:
        return self.state_dir / "runs.sqlite"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"


def get_settings() -> Settings:
    return Settings()
