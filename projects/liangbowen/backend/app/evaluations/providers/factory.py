from app.core.config import Settings
from app.evaluations.providers.base import EvaluationProvider
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.providers.openai_compatible import OpenAICompatibleProvider


def create_evaluation_provider(settings: Settings) -> EvaluationProvider:
    """Build exactly the provider selected by validated application settings."""
    if type(settings) is not Settings:
        raise TypeError("settings must be Settings")
    if settings.agent_provider == "mock":
        return MockEvaluationProvider()
    api_key = settings.agent_api_key
    if api_key is None:  # pragma: no cover - Settings validation owns this contract
        raise ValueError("agent provider configuration is invalid")
    return OpenAICompatibleProvider(
        base_url=settings.agent_base_url,
        api_key=api_key.get_secret_value(),
        model=settings.agent_model,
        response_format=settings.agent_response_format,
        timeout=settings.agent_timeout_seconds,
        allow_insecure_http=settings.agent_allow_insecure_http,
    )
