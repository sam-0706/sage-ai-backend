from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    """All configuration comes from the environment. Secrets never leave the server."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "preview", "production"] = "development"
    app_name: str = "SAGE AI API"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    public_base_url: str = ""  # e.g. https://sage-ai-backend.vercel.app — used to build webhook URLs

    # Database (Supabase Postgres via the transaction pooler)
    database_url: str
    db_pool_min: int = 0
    db_pool_max: int = 5
    db_command_timeout: float = 15.0

    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Clerk
    clerk_secret_key: str
    clerk_publishable_key: str = ""
    clerk_jwks_url: str = ""  # derived from publishable key when empty
    clerk_issuer: str = ""  # derived from publishable key when empty
    clerk_authorized_parties: CsvList = Field(default_factory=list)

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-5-mini"
    openai_chat_model: str = ""  # defaults to openai_model
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_s: float = 45.0
    openai_max_retries: int = 2

    # OmniDimension voice
    omnidim_api_key: str = ""
    omnidim_base_url: str = "https://omnidim.io/api/v1"
    omnidim_agent_id: str = ""
    omnidim_from_number_id: str = ""
    omnidim_webhook_secret: str = ""
    voice_expected_duration_sec: int = 180
    voice_allowed_test_numbers: CsvList = Field(default_factory=list)
    voice_allow_own_number: bool = True

    # Razorpay (test mode only in the beta)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Operations
    cron_secret: str = ""
    superadmin_emails: CsvList = Field(default_factory=list)
    cors_origins: CsvList = Field(default_factory=list)
    dev_auth_bypass: bool = False  # local testing only; refused outside development

    @field_validator(
        "superadmin_emails", "cors_origins", "clerk_authorized_parties", "voice_allowed_test_numbers", mode="before"
    )
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def chat_model(self) -> str:
        return self.openai_chat_model or self.openai_model

    @property
    def razorpay_test_mode(self) -> bool:
        return self.razorpay_key_id.startswith("rzp_test_")


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    if s.dev_auth_bypass and s.app_env != "development":
        raise RuntimeError("DEV_AUTH_BYPASS may only be enabled when APP_ENV=development")
    if s.razorpay_key_id and not s.razorpay_test_mode:
        raise RuntimeError("Only Razorpay test keys are permitted in the beta (PRD Epic 9)")
    return s
