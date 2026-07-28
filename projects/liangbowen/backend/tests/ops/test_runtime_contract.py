from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health/ready":
            self.send_error(404)
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _readiness_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|^volumes:\n|\Z)",
        compose,
    )
    assert match is not None, f"missing Compose service: {service_name}"
    return match.group("body")


def test_environment_example_separates_host_and_compose_addresses() -> None:
    example = _read(".env.example")

    assert "APP_ENV=development" in example
    assert "DATABASE_URL=postgresql+asyncpg://grader:grader@localhost:5432/grader" in example
    assert "COMPOSE_DATABASE_URL=postgresql+asyncpg://grader:grader@postgres:5432/grader" in example
    assert (
        "TEST_DATABASE_URL=postgresql+asyncpg://grader:grader@localhost:5433/grader_test" in example
    )
    assert "REDIS_URL=redis://localhost:6379/0" in example
    assert "COMPOSE_REDIS_URL=redis://redis:6379/0" in example
    assert "JWT_SECRET=development-secret-change-me" in example
    assert "JWT_EXP_MINUTES=60" in example
    assert "AGENT_PROVIDER=mock" in example
    assert "AGENT_RESPONSE_FORMAT=json-schema" in example
    assert "CORS_ORIGINS='[\"http://localhost:5173\"]'" in example
    assert "POSTGRES_PORT=5432" in example
    assert "TEST_POSTGRES_PORT=5433" in example
    assert "REDIS_PORT=6379" in example
    assert "API_PORT=8000" in example
    assert "If POSTGRES_PORT or TEST_POSTGRES_PORT changes" in example
    assert "update the matching host URL" in example
    assert "replace" in example.casefold()


def test_agent_response_format_reaches_all_grading_services() -> None:
    compose = _read("docker-compose.yml")

    for service_name in ("api", "worker", "beat"):
        service = _service_block(compose, service_name)
        assert "AGENT_RESPONSE_FORMAT: ${AGENT_RESPONSE_FORMAT:-json-schema}" in service


def test_gitignore_protects_local_state_without_hiding_contract_or_migrations() -> None:
    ignored = [
        ".env",
        ".env.local",
        ".venv/bin/python",
        "backend/.coverage",
        "frontend/dist/app.js",
    ]
    tracked_contracts = [
        ".env.example",
        "backend/migrations/versions/0001_domain.py",
        "backend/alembic.ini",
    ]

    for path in ignored:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", path],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, f"expected ignored path: {path}"
    for path in tracked_contracts:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", path],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, f"must not ignore: {path}"


def test_backend_dockerfile_installs_package_and_runs_as_non_root() -> None:
    dockerfile = _read("backend/Dockerfile")

    assert dockerfile.startswith("FROM python:3.12-slim AS builder")
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    runtime = dockerfile.split("FROM python:3.12-slim AS runtime", maxsplit=1)[1]
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "COPY pyproject.toml" in dockerfile
    assert "COPY app" in dockerfile
    assert "COPY fixtures" in dockerfile
    assert "COPY migrations" in dockerfile
    assert "COPY scripts" in dockerfile
    assert "pip wheel" in dockerfile
    assert "COPY --from=builder /wheels /wheels" in runtime
    assert "COPY app" not in runtime
    assert "COPY scripts" not in runtime
    assert "COPY pyproject.toml" not in runtime
    assert "COPY --chown=root:root alembic.ini" in runtime
    assert "COPY --chown=root:root migrations" in runtime
    assert "chmod -R a-w /app/alembic.ini /app/migrations" in runtime
    assert re.search(r"(?m)^USER (?!root\b)", dockerfile)
    assert 'ENTRYPOINT ["tini", "--"]' in dockerfile
    assert 'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile


def test_dockerignore_excludes_secrets_and_build_noise() -> None:
    dockerignore = _read("backend/.dockerignore")

    for pattern in (
        ".env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "*.pem",
        "*.key",
    ):
        assert pattern in dockerignore


def test_compose_default_runtime_has_healthy_dependencies_and_safe_storage() -> None:
    compose = _read("docker-compose.yml")
    postgres = _service_block(compose, "postgres")
    test_postgres = _service_block(compose, "test-postgres")
    redis = _service_block(compose, "redis")
    api = _service_block(compose, "api")

    assert "postgres:16" in postgres
    assert '"127.0.0.1:${POSTGRES_PORT:-5432}:5432"' in postgres
    assert "postgres-data:/var/lib/postgresql/data" in postgres
    assert "healthcheck:" in postgres
    assert "postgres:16" in test_postgres
    assert '"127.0.0.1:${TEST_POSTGRES_PORT:-5433}:5432"' in test_postgres
    assert "tmpfs:" in test_postgres
    assert "postgres-data" not in test_postgres
    assert "redis:7" in redis
    assert '"127.0.0.1:${REDIS_PORT:-6379}:6379"' in redis
    assert "healthcheck:" in redis
    assert "context: ./backend" in api
    assert '"127.0.0.1:${API_PORT:-8000}:8000"' in api
    assert "COMPOSE_DATABASE_URL" in api
    assert "COMPOSE_REDIS_URL" in api
    assert re.search(r"postgres:\n\s+condition: service_healthy", api)
    assert re.search(r"redis:\n\s+condition: service_healthy", api)
    assert "alembic upgrade head" not in api
    assert "exec uvicorn" in api
    assert "/health/ready" in api
    assert "urllib.request" in api
    assert "ProxyHandler({})" in api
    assert "curl" not in api


def test_a7_mattermost_secrets_are_placeholders_and_api_only() -> None:
    example = _read(".env.example")
    compose = _read("docker-compose.yml")
    api = _service_block(compose, "api")
    worker = _service_block(compose, "worker")
    beat = _service_block(compose, "beat")

    assert "# MATTERMOST_COMMAND_TOKEN=replace-with-slash-command-token" in example
    assert "# MATTERMOST_DEMO_SETUP_KEY=replace-with-random-demo-key" in example
    assert "MATTERMOST_COMMAND_TOKEN: ${MATTERMOST_COMMAND_TOKEN:-}" in api
    assert "MATTERMOST_DEMO_SETUP_KEY: ${MATTERMOST_DEMO_SETUP_KEY:-}" in api
    assert "MATTERMOST_COMMAND_TOKEN" not in worker
    assert "MATTERMOST_DEMO_SETUP_KEY" not in worker
    assert "MATTERMOST_COMMAND_TOKEN" not in beat
    assert "MATTERMOST_DEMO_SETUP_KEY" not in beat


def test_a8_mattermost_secrets_follow_runtime_least_privilege() -> None:
    example = _read(".env.example")
    compose = _read("docker-compose.yml")
    api = _service_block(compose, "api")
    worker = _service_block(compose, "worker")
    beat = _service_block(compose, "beat")

    assert "# MATTERMOST_BOT_TOKEN=replace-with-bot-token" in example
    assert "MATTERMOST_ACTION_SECRET" in api
    assert "MATTERMOST_BOT_TOKEN" not in api
    assert "MATTERMOST_BOT_TOKEN" in worker
    assert "MATTERMOST_ACTION_SECRET" in worker
    assert "MATTERMOST_BOT_TOKEN" not in beat
    assert "MATTERMOST_ACTION_SECRET" not in beat


def test_installed_wheel_smoke_checks_mattermost_route_gating() -> None:
    verifier = _read("backend/scripts/verify_wheel_routes.py")

    assert '"/api/v1/integrations/mattermost/commands"' in verifier
    assert '"/api/v1/integrations/mattermost/actions"' in verifier
    assert '"/api/v1/integrations/mattermost/demo-bindings"' in verifier
    assert 'app_env="test"' in verifier
    assert 'app_env="production"' in verifier
    assert "assert MATTERMOST_DEMO_PATH not in production_paths" in verifier
    assert "MattermostClient" in verifier
    assert "httpx.MockTransport" in verifier
    assert "render_report_card" in verifier
    assert "parse_and_verify_action" in verifier


def test_worker_and_beat_run_in_default_runtime_with_dedicated_queues() -> None:
    compose = _read("docker-compose.yml")
    worker = _service_block(compose, "worker")
    beat = _service_block(compose, "beat")

    assert "profiles:" not in worker
    assert "celery -A app.evaluations.worker.celery_app worker --loglevel=INFO" in worker
    assert "-Q evaluations,notifications,maintenance" in worker
    assert re.search(r"postgres:\n\s+condition: service_healthy", worker)
    assert re.search(r"redis:\n\s+condition: service_healthy", worker)
    assert "profiles:" not in beat
    assert (
        "celery -A app.evaluations.worker.celery_app beat --loglevel=INFO "
        "--schedule=/tmp/celerybeat-schedule --pidfile=/tmp/celerybeat.pid"
    ) in beat
    assert re.search(r"postgres:\n\s+condition: service_healthy", beat)
    assert re.search(r"redis:\n\s+condition: service_healthy", beat)
    assert (ROOT / "backend/app/evaluations/worker.py").is_file()


def test_makefile_exposes_repeatable_backend_workflow() -> None:
    aliases = {
        "test": "test-backend",
        "lint": "lint-backend",
        "format": "format-backend",
        "build": "build-backend",
    }
    required_targets = {
        "help",
        "install",
        "env",
        "db-up",
        "db-down",
        "migrate",
        "seed",
        "api-up",
        "api-down",
        "health",
        "test-backend",
        "lint-backend",
        "format-backend",
        "build-backend",
        "verify-backend",
        "verify",
        "verify-local",
        "foundation",
        *aliases,
    }
    makefile = _read("Makefile")
    targets = set(re.findall(r"(?m)^([a-z][a-z0-9-]*):", makefile))

    assert required_targets <= targets
    assert "python3.12" in makefile
    assert "TEST_DATABASE_URL" in makefile
    assert "MIGRATION_LEGACY_PROCESSES_STOPPED=true" in makefile
    assert not re.search(
        r"(?m)^MIGRATION_LEGACY_PROCESSES_STOPPED=true\s*$",
        _read(".env.example"),
    )
    assert "localhost:5433/grader_test" in makefile
    assert "PYTHONPATH" in makefile
    seed_script = _read("backend/scripts/seed_demo.py")
    assert "APP_ENV" not in makefile
    assert 'not in {"development", "test"}' in seed_script
    assert "ProxyHandler({})" in _read("backend/scripts/healthcheck.py")
    assert '. "$(ENV_FILE)"' not in makefile
    assert 'source "$(ENV_FILE)"' not in makefile
    assert "scripts.env_exec" in makefile
    assert "scripts.healthcheck" in makefile
    assert '--wheel-dir backend/dist "$(BACKEND_DIR)"' in makefile
    assert "api-up: env ## Build and start the API, evaluation worker, and redrive beat" in makefile
    assert (
        "api-down: env ## Stop and remove the API, evaluation worker, and redrive beat containers"
        in makefile
    )
    api_up = subprocess.run(
        ["make", "--no-print-directory", "-n", "api-up"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    api_down = subprocess.run(
        ["make", "--no-print-directory", "-n", "api-down"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    migrate = subprocess.run(
        ["make", "--no-print-directory", "-n", "migrate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "up -d --build --wait api worker beat" in api_up.stdout
    assert "rm --stop --force api worker beat" in api_down.stdout
    assert "MIGRATION_LEGACY_PROCESSES_STOPPED=true" not in migrate.stdout
    for alias, backend_target in aliases.items():
        assert re.search(rf"(?m)^{alias}: {backend_target}(?:\s+## .+)?$", makefile)
    assert re.search(r"(?m)^verify:.*\n\t.*scripts/verify_acceptance\.py$", makefile)
    assert re.search(
        r"(?m)^verify-local:.*\n\t.*scripts/verify_acceptance\.py --local-only$",
        makefile,
    )

    phony = re.search(r"(?ms)^\.PHONY:(?P<body>.*?)(?=^[^\t ].*?:|\Z)", makefile)
    assert phony is not None
    for alias in (*aliases, "verify", "verify-local"):
        assert re.search(rf"(?:^|\s){alias}(?:\s|$)", phony.group("body"))

    for target in sorted(required_targets):
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"make -n {target}:\n{result.stdout}{result.stderr}"


def test_makefile_resolves_its_root_from_external_working_directories(tmp_path: Path) -> None:
    external_cwd = tmp_path / "external cwd with spaces"
    external_cwd.mkdir()
    makefile = ROOT / "Makefile"

    help_result = subprocess.run(
        ["make", "--no-print-directory", "-f", str(makefile), "help"],
        cwd=external_cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    for alias in ("test", "lint", "format", "build", "verify"):
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-C",
                str(external_cwd),
                "-f",
                str(makefile),
                "-n",
                alias,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"external make -n {alias}:\n{result.stdout}{result.stderr}"
        assert str(ROOT) in result.stdout


def test_make_health_reads_api_port_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "runtime env with spaces"
    with _readiness_server() as api_port:
        env_file.write_text(f"API_PORT={api_port}\n", encoding="utf-8")
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                "health",
                f"ENV_FILE={env_file}",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == '{"status":"ok"}'


def test_make_health_command_line_port_takes_precedence_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("API_PORT=1\n", encoding="utf-8")
    with _readiness_server() as api_port:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                "health",
                f"ENV_FILE={env_file}",
                f"API_PORT={api_port}",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == '{"status":"ok"}'


def test_make_health_dry_run_handles_missing_env_and_foundation_override(tmp_path: Path) -> None:
    missing_env = tmp_path / "missing env"
    makefile = ROOT / "Makefile"
    default_result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(makefile),
            "-n",
            "health",
            f"ENV_FILE={missing_env}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    override_result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(makefile),
            "-n",
            "foundation",
            f"ENV_FILE={missing_env}",
            "API_PORT=18081",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert default_result.returncode == 0, default_result.stdout + default_result.stderr
    assert "scripts.healthcheck" in default_result.stdout
    assert "DEFAULT_API_PORT = 8000" in _read("backend/scripts/healthcheck.py")
    assert override_result.returncode == 0, override_result.stdout + override_result.stderr
    assert "scripts.healthcheck" in override_result.stdout
    migration_lines = [
        line for line in override_result.stdout.splitlines() if line.rstrip().endswith("migrate")
    ]
    assert len(migration_lines) == 1
    assert migration_lines[0].startswith("MIGRATION_LEGACY_PROCESSES_STOPPED=true ")


def test_foundation_is_serial_under_parallel_make_and_stops_on_failure(tmp_path: Path) -> None:
    makefile_text = _read("Makefile")
    assert re.search(r"(?m)^foundation:\s+## ", makefile_text)
    assert makefile_text.count('+@"$(MAKE)" --no-print-directory') >= 9

    marker = tmp_path / "foundation marker"
    fake_make = tmp_path / "fake make with spaces"
    fake_make.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "step = sys.argv[-1]\n"
        "marker = os.environ['FOUNDATION_MARKER']\n"
        "with open(marker, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(f'start:{step}\\n')\n"
        "time.sleep(0.02)\n"
        "if step == os.environ.get('FOUNDATION_FAIL_STEP'):\n"
        "    raise SystemExit(7)\n"
        "with open(marker, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(f'done:{step}\\n')\n",
        encoding="utf-8",
    )
    fake_make.chmod(0o755)
    steps = ["install", "env", "db-up", "migrate", "seed", "verify", "build", "api-up", "health"]
    environment = dict(os.environ, FOUNDATION_MARKER=str(marker))

    result = subprocess.run(
        ["make", "--no-print-directory", "-j8", "foundation", f"MAKE={fake_make}"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8").splitlines() == [
        event for step in steps for event in (f"start:{step}", f"done:{step}")
    ]

    marker.write_text("", encoding="utf-8")
    environment["FOUNDATION_FAIL_STEP"] = "seed"
    failed = subprocess.run(
        ["make", "--no-print-directory", "-j8", "foundation", f"MAKE={fake_make}"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "start:install",
        "done:install",
        "start:env",
        "done:env",
        "start:db-up",
        "done:db-up",
        "start:migrate",
        "done:migrate",
        "start:seed",
    ]
