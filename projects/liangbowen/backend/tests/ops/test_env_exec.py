from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"


def test_python_dotenv_is_an_explicit_runtime_dependency() -> None:
    pyproject = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")

    assert '"python-dotenv>=1.0"' in pyproject


def _run_env_exec(
    env_file: Path,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    child_environment = dict(os.environ)
    child_environment["PYTHONPATH"] = str(BACKEND)
    if environment:
        child_environment.update(environment)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.env_exec",
            "--env-file",
            str(env_file),
            "--cwd",
            str(env_file.parent),
            "--",
            *command,
        ],
        cwd=ROOT,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _touch_command(marker: Path) -> list[str]:
    return [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]


def test_hostile_dotenv_values_are_data_and_never_shell_commands(tmp_path: Path) -> None:
    marker = tmp_path / "must not exist"
    env_file = tmp_path / "hostile env"
    env_file.write_text(
        "\n".join(
            [
                f"DOLLAR=$(touch '{marker}')",
                f"BACKTICK=`touch '{marker}'`",
                f"SEMICOLON=hello; touch '{marker}'",
                'QUOTED="hello # world"',
                "SPACED=hello world # ignored comment",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    probe = (
        "import json, os; "
        "print(json.dumps({key: os.environ[key] for key in "
        "('DOLLAR', 'BACKTICK', 'SEMICOLON', 'QUOTED', 'SPACED')}))"
    )

    result = _run_env_exec(env_file, [sys.executable, "-c", probe])

    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(result.stdout)
    assert values["DOLLAR"].startswith("$(touch ")
    assert values["BACKTICK"].startswith("`touch ")
    assert values["SEMICOLON"].startswith("hello; touch ")
    assert values["QUOTED"] == "hello # world"
    assert values["SPACED"] == "hello world"
    assert not marker.exists()


def test_process_environment_takes_precedence_over_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=development\n", encoding="utf-8")

    result = _run_env_exec(
        env_file,
        [sys.executable, "-c", "import os; print(os.environ['APP_ENV'])"],
        environment={"APP_ENV": "production"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "production"


def test_invalid_dotenv_key_is_rejected_without_running_command_or_leaking_value(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "command-ran"
    secret = "do-not-print-this-secret"
    env_file = tmp_path / ".env"
    env_file.write_text(f"BAD-KEY={secret}\n", encoding="utf-8")

    result = _run_env_exec(
        env_file,
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
    )

    assert result.returncode == 2
    assert "invalid environment key" in result.stderr
    assert secret not in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "dotenv_text",
    [
        'APP_ENV="production\nDATABASE_URL=postgresql://safe\n',
        "APP_ENV='production\nDATABASE_URL=postgresql://safe\n",
        "APP_ENV=production\nthis is not dotenv !!!\nDATABASE_URL=postgresql://safe\n",
        'DATABASE_URL="postgresql://grader:secret@localhost/grader\nAPP_ENV=production\n',
        'API_PORT="18081\nAPP_ENV=production\n',
        "APP_ENV=production\nDATABASE_URL=postgresql://safe\ninvalid garbage after valid data\n",
    ],
    ids=[
        "unclosed-double-app-env",
        "unclosed-single-app-env",
        "garbage-between-critical-values",
        "unclosed-database-url",
        "unclosed-api-port",
        "valid-prefix-invalid-suffix",
    ],
)
def test_malformed_dotenv_never_starts_command(
    tmp_path: Path,
    dotenv_text: str,
) -> None:
    marker = tmp_path / "command-must-not-run"
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv_text, encoding="utf-8")

    result = _run_env_exec(env_file, _touch_command(marker))

    assert result.returncode == 2
    assert "malformed dotenv file" in result.stderr
    assert "secret" not in result.stderr
    assert "postgresql" not in result.stderr
    assert not marker.exists()


def test_malformed_production_app_env_cannot_fall_back_to_development(tmp_path: Path) -> None:
    marker = tmp_path / "development-command-ran"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV='production\n"
        "JWT_SECRET=production-secret-with-at-least-32-bytes\n"
        "DATABASE_URL=postgresql+asyncpg://grader:secret@127.0.0.1:59999/grader\n",
        encoding="utf-8",
    )

    result = _run_env_exec(env_file, _touch_command(marker))

    assert result.returncode == 2
    assert "malformed dotenv file" in result.stderr
    assert "production" not in result.stderr
    assert "secret" not in result.stderr
    assert not marker.exists()


def test_malformed_api_port_is_rejected_before_healthcheck_fallback(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('API_PORT="18081\n', encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(BACKEND))

    result = subprocess.run(
        [sys.executable, "-m", "scripts.healthcheck", "--env-file", str(env_file)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "health check failed (DotenvContractError)" in result.stderr
    assert "18081" not in result.stderr


def test_valid_crlf_comments_empty_values_and_missing_final_newline_are_supported(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"# comment\r\nEMPTY=\r\nBARE\r\nQUOTED='hello # world'\r\nLAST=tail")
    probe = (
        "import json, os; print(json.dumps({key: os.environ[key] for key in "
        "('EMPTY', 'BARE', 'QUOTED', 'LAST')}))"
    )

    result = _run_env_exec(env_file, [sys.executable, "-c", probe])

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "EMPTY": "",
        "BARE": "",
        "QUOTED": "hello # world",
        "LAST": "tail",
    }


def test_dotenv_interpolation_is_disabled_and_documented(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BASE=expanded\nLITERAL=${BASE}\n", encoding="utf-8")

    result = _run_env_exec(
        env_file,
        [sys.executable, "-c", "import os; print(os.environ['LITERAL'])"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "${BASE}"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "Host commands do not expand ${VAR}; use literal values" in example


@pytest.mark.parametrize("failure_kind", ["invalid-utf8", "symlink-loop"])
def test_unreadable_dotenv_never_starts_command(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    marker = tmp_path / "command-must-not-run"
    env_file = tmp_path / ".env"
    if failure_kind == "invalid-utf8":
        env_file.write_bytes(b"APP_ENV=development\nJWT_SECRET=\xff\n")
    else:
        env_file.symlink_to(env_file.name)

    result = _run_env_exec(env_file, _touch_command(marker))

    assert result.returncode == 2
    assert "dotenv file could not be read" in result.stderr
    assert not marker.exists()


def test_production_seed_refuses_before_attempting_database_connection(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=production\n"
        "JWT_SECRET=production-secret-with-at-least-32-bytes\n"
        "DATABASE_URL=postgresql+asyncpg://grader:secret@127.0.0.1:59999/grader\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = _run_env_exec(env_file, [sys.executable, "-m", "scripts.seed_demo"])
    elapsed = time.monotonic() - started

    assert result.returncode == 1
    assert "disabled outside development and test" in result.stderr
    assert elapsed < 2
    assert "secret" not in result.stderr


@pytest.mark.parametrize("api_port", ["0", "65536", "not-a-port"])
def test_healthcheck_rejects_invalid_api_port_without_echoing_value(
    tmp_path: Path,
    api_port: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"API_PORT={api_port}\n", encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(BACKEND))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.healthcheck",
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "health check failed (ValueError)" in result.stderr
    assert api_port not in result.stderr
