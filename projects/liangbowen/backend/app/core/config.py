from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.integrations.mattermost.ids import validate_mattermost_id
from app.integrations.mattermost.urls import validate_http_url, validate_required_card_urls


class Settings(BaseSettings):
    app_env: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = "postgresql+asyncpg://grader:grader@localhost:5432/grader"
    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    jwt_secret: SecretStr = SecretStr("development-secret-change-me")
    jwt_exp_minutes: int = Field(default=60, ge=5, le=1440)
    cors_origins: list[str] = ["http://localhost:5173"]
    agent_provider: Literal["mock", "openai-compatible"] = "mock"
    agent_mock_fixture: Literal["complete", "partial", "incorrect"] = "partial"
    agent_base_url: str = "https://api.openai.com/v1"
    agent_model: str = "grader-v1"
    agent_api_key: SecretStr | None = None
    agent_response_format: Literal["json-schema", "json-object"] = "json-schema"
    agent_allow_insecure_http: bool = False
    agent_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    celery_task_always_eager: bool = False
    evaluation_dispatch_enabled: bool = False
    mattermost_command_token: SecretStr | None = None
    mattermost_demo_setup_key: SecretStr | None = None
    mattermost_url: str | None = None
    mattermost_bot_token: SecretStr | None = None
    mattermost_bot_user_id: str | None = None
    mattermost_action_secret: SecretStr | None = None
    mattermost_action_url: str | None = None
    web_console_url: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator(
        "mattermost_command_token",
        "mattermost_demo_setup_key",
        "mattermost_bot_token",
        "mattermost_action_secret",
        mode="before",
    )
    @classmethod
    def normalize_optional_mattermost_secret(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return None
            return SecretStr(value)
        return value

    @field_validator(
        "mattermost_command_token",
        "mattermost_demo_setup_key",
        "mattermost_bot_token",
        "mattermost_action_secret",
    )
    @classmethod
    def reject_invalid_mattermost_secret(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is not None and "\0" in value.get_secret_value():
            raise ValueError("Mattermost secret contains an invalid character")
        return value

    @field_validator(
        "mattermost_url",
        "mattermost_bot_user_id",
        "mattermost_action_url",
        "web_console_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_mattermost_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("mattermost_bot_user_id")
    @classmethod
    def validate_mattermost_bot_user_id(cls, value: str | None) -> str | None:
        if value is not None:
            return validate_mattermost_id(value, name="Mattermost Bot user ID")
        return value

    @model_validator(mode="after")
    def reject_development_secret_outside_safe_environments(self) -> Self:
        if self.app_env not in {"development", "test"}:
            jwt_secret = self.jwt_secret.get_secret_value()
            if jwt_secret == "development-secret-change-me":
                raise ValueError("development JWT secret is only allowed in development or test")
            if len(jwt_secret.encode("utf-8")) < 32:
                raise ValueError(
                    "JWT secret must be at least 32 UTF-8 bytes in staging or production"
                )
        if self.agent_provider == "openai-compatible" and (
            self.agent_api_key is None or not self.agent_api_key.get_secret_value()
        ):
            raise ValueError("agent_api_key is required for the selected provider")
        for name in ("mattermost_url", "mattermost_action_url", "web_console_url"):
            value = getattr(self, name)
            if value is None:
                continue
            setattr(
                self,
                name,
                validate_http_url(
                    value,
                    name=name,
                    allow_insecure_http=self.app_env in {"development", "test"},
                ),
            )
        if self.mattermost_action_url is not None and not self.mattermost_action_url.rstrip(
            "/"
        ).endswith("/api/v1/integrations/mattermost/actions"):
            raise ValueError("mattermost_action_url must target the action endpoint")
        if self.mattermost_action_url is not None and self.web_console_url is not None:
            self.mattermost_action_url, self.web_console_url = validate_required_card_urls(
                self.mattermost_action_url,
                self.web_console_url,
                allow_insecure_http=self.app_env in {"development", "test"},
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
