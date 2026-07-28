import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings

SETTING_ENV_NAMES = {
    "app_env",
    "database_url",
    "redis_url",
    "jwt_secret",
    "jwt_exp_minutes",
    "cors_origins",
    "agent_provider",
    "agent_mock_fixture",
    "agent_base_url",
    "agent_model",
    "agent_api_key",
    "agent_allow_insecure_http",
    "agent_timeout_seconds",
    "celery_task_always_eager",
    "evaluation_dispatch_enabled",
}


def clean_subprocess_env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if key.casefold() not in SETTING_ENV_NAMES
    }


def run_test_in_subprocess(
    test_name: str,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).resolve()}::{test_name}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_lowercase_environment_cannot_contaminate_defaults(tmp_path: Path) -> None:
    ambient_dir = tmp_path / "lowercase-environment"
    ambient_dir.mkdir()
    env = clean_subprocess_env()
    env.update(
        {
            "app_env": "production",
            "database_url": "postgresql+asyncpg://hostile/hostile",
            "redis_url": "redis://hostile:6379/9",
            "jwt_secret": "hostile-production-secret",
            "jwt_exp_minutes": "1",
            "cors_origins": '["https://hostile.example"]',
        }
    )

    result = run_test_in_subprocess("test_default_settings", ambient_dir, env)

    assert result.returncode == 0, result.stdout + result.stderr


def test_local_dotenv_cannot_abort_app_import(tmp_path: Path) -> None:
    ambient_dir = tmp_path / "local-dotenv"
    ambient_dir.mkdir()
    (ambient_dir / ".env").write_text(
        "APP_ENV=production\nJWT_SECRET=development-secret-change-me\n",
        encoding="utf-8",
    )

    result = run_test_in_subprocess("test_liveness", ambient_dir, clean_subprocess_env())

    assert result.returncode == 0, result.stdout + result.stderr


def test_default_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.database_url == "postgresql+asyncpg://grader:grader@localhost:5432/grader"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.jwt_secret.get_secret_value() == "development-secret-change-me"
    assert settings.jwt_exp_minutes == 60
    assert settings.cors_origins == ["http://localhost:5173"]


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_safe_environments_allow_documented_default_jwt_secret(app_env: str) -> None:
    settings = Settings(app_env=app_env, _env_file=None)

    assert settings.jwt_secret.get_secret_value() == "development-secret-change-me"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_default_jwt_secret_rejected_outside_safe_environments(app_env: str) -> None:
    with pytest.raises(ValidationError, match="development JWT secret"):
        Settings(app_env=app_env, _env_file=None)


def test_strong_non_default_jwt_secret_accepted_in_production() -> None:
    settings = Settings(
        app_env="production",
        jwt_secret="production-secret-with-at-least-32-bytes",
        _env_file=None,
    )

    assert settings.jwt_secret.get_secret_value() == "production-secret-with-at-least-32-bytes"


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize(
    "jwt_secret",
    [
        "short-production-secret",
        "x" * 31,
    ],
)
def test_weak_jwt_secret_rejected_outside_safe_environments(
    app_env: str,
    jwt_secret: str,
) -> None:
    with pytest.raises(ValidationError, match="at least 32 UTF-8 bytes"):
        Settings(app_env=app_env, jwt_secret=jwt_secret, _env_file=None)


@pytest.mark.parametrize("app_env", ["local", "prod", "qa", ""])
def test_invalid_app_environment_rejected(app_env: str) -> None:
    with pytest.raises(ValidationError):
        Settings(app_env=app_env, _env_file=None)


@pytest.mark.parametrize("jwt_exp_minutes", [-1, 0, 1, 4, 1441, 10_000])
def test_jwt_expiry_outside_safe_bounds_rejected(jwt_exp_minutes: int) -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_exp_minutes=jwt_exp_minutes, _env_file=None)


@pytest.mark.parametrize("jwt_exp_minutes", [5, 60, 1440])
def test_jwt_expiry_safe_bounds_accepted(jwt_exp_minutes: int) -> None:
    settings = Settings(jwt_exp_minutes=jwt_exp_minutes, _env_file=None)

    assert settings.jwt_exp_minutes == jwt_exp_minutes


def test_api_metadata() -> None:
    from app.main import create_app

    app = create_app()

    assert app.title == "AI Grading API"
    assert app.version == "0.1.0"


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("database_ready", "expected_status", "expected_body"),
    [
        (True, 200, {"status": "ok"}),
        (False, 503, {"status": "unavailable"}),
    ],
)
async def test_readiness_reflects_database_availability(
    monkeypatch: pytest.MonkeyPatch,
    database_ready: bool,
    expected_status: int,
    expected_body: dict[str, str],
) -> None:
    import app.main as main_module

    async def database_probe() -> bool:
        return database_ready

    monkeypatch.setattr(main_module, "database_is_ready", database_probe)
    application = main_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as readiness_client:
        response = await readiness_client.get("/health/ready")

    assert response.status_code == expected_status
    assert response.json() == expected_body


async def test_api_root(client: AsyncClient) -> None:
    response = await client.get("/api/v1")

    assert response.status_code == 200
    assert response.json() == {"name": "AI Grading API", "version": "v1"}


async def test_cors_preflight_allows_local_frontend(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"
