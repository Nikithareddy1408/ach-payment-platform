"""Settings, read from environment variables (or a .env file) and validated at startup.

A typo or missing value stops the service immediately with a clear message,
instead of failing later in production.
"""
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql://ach:ach@localhost:5432/ach"
    database_pool_size: int = Field(20, ge=1)
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Banking partner
    bank_base_url: str = "http://localhost:4000"
    bank_timeout_ms: int = Field(5000, ge=1)

    # Payment processing
    worker_concurrency: int = Field(4, ge=1)
    max_attempts: int = Field(6, ge=1)
    retry_base_ms: int = Field(2000, ge=1)
    retry_max_ms: int = Field(300_000, ge=1)
    job_lease_ms: int = Field(60_000, ge=1)
    poll_interval_ms: int = Field(250, ge=1)

    # Circuit breaker around the bank
    breaker_failure_threshold: int = Field(5, ge=1)
    breaker_cooldown_ms: int = Field(30_000, ge=1)

    # Webhooks
    webhook_timeout_ms: int = Field(5000, ge=1)
    webhook_max_attempts: int = Field(8, ge=1)
    webhook_retry_base_ms: int = Field(5000, ge=1)
    webhook_retry_max_ms: int = Field(3_600_000, ge=1)
    webhook_concurrency: int = Field(4, ge=1)
    webhook_block_private_ips: bool = True

    # Limits
    max_amount_cents: int = Field(100_000_000, ge=1)  # $1,000,000.00
    rate_limit_per_minute: int = Field(600, ge=1)

    # Sandbox bank
    mock_bank_port: int = 4000
    mock_bank_failure_rate: float = Field(0.1, ge=0, le=1)

    @model_validator(mode="after")
    def lease_outlives_bank_call(self) -> "Settings":
        if self.job_lease_ms <= self.bank_timeout_ms * 2:
            raise ValueError("JOB_LEASE_MS must be more than twice BANK_TIMEOUT_MS, or a slow bank call could outlive its lease")
        return self

    @property
    def webhook_lease_ms(self) -> int:
        return self.webhook_timeout_ms * 3
