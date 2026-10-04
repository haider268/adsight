"""Runtime settings, read from ADSIGHT_* environment variables and an optional .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADSIGHT_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    seed: int = 7
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "adsight.duckdb"


def get_settings() -> Settings:
    return Settings()
