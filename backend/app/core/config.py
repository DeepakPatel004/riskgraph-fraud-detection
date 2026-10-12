from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal


class Settings(BaseSettings):
    app_name: str = "RiskGraph API"
    database_url: str = "sqlite:///./riskgraph.db"
    # Inert until a NeptuneClient is explicitly constructed; endpoints are unchanged.
    neptune_endpoint: str | None = None
    neptune_port: int = Field(default=8182, ge=1, le=65535)
    neptune_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    neptune_iam_auth_enabled: bool = True
    neptune_region: str | None = None
    graph_sync_adapter: Literal["disabled", "memory", "neptune"] = "disabled"
    graph_sync_initial_delay: float = Field(default=5, gt=0, allow_inf_nan=False)
    graph_sync_backoff_factor: float = Field(default=2, ge=1, allow_inf_nan=False)
    graph_sync_max_delay: float = Field(default=300, gt=0, allow_inf_nan=False)
    graph_sync_max_attempts: int = Field(default=5, ge=1)
    graph_sync_lease_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    graph_sync_poll_interval: float = Field(default=1, gt=0, allow_inf_nan=False)
    graph_sync_batch_size: int = Field(default=20, ge=1)
    fraud_scoring_enabled: bool = False
    # Operator attestation for a dedicated, reconciled graph; never auto-enabled.
    graph_population_verified: bool = False
    graph_population_name: str | None = Field(default=None, min_length=1, max_length=128)
    scoring_api_workers: int = Field(default=1, ge=1)
    fraud_model_artifact_dir: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
