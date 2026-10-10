from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    app_name: str = "RiskGraph API"
    database_url: str = "sqlite:///./riskgraph.db"
    # Inert until a NeptuneClient is explicitly constructed; endpoints are unchanged.
    neptune_endpoint: str | None = None
    neptune_port: int = Field(default=8182, ge=1, le=65535)
    neptune_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    neptune_iam_auth_enabled: bool = True
    neptune_region: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
