import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
MATTERMOST_LOCAL_PASSWORD = "mm-local-9Jv7wQ2xR5cN8kT4"


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_default_runtime_has_health_checked_dependencies() -> None:
    services = _compose()["services"]

    assert {"api", "worker", "web", "postgres", "redis"} <= services.keys()
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-8080}:80"]
    assert services["web"]["build"]["context"] == "./frontend"

    for service_name in ("postgres", "redis", "api", "web"):
        assert "healthcheck" in services[service_name]


def test_beat_writes_runtime_state_to_a_writable_directory() -> None:
    command = _compose()["services"]["beat"]["command"]

    assert "--schedule=/tmp/celerybeat-schedule" in command
    assert "--pidfile=/tmp/celerybeat.pid" in command


def test_frontend_image_serves_spa_health_and_api_proxy() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "FROM node:22.22.0-alpine AS build" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "ARG VITE_API_BASE_URL=/api/v1" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "FROM nginx:1.27.5-alpine" in dockerfile
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile
    assert "HEALTHCHECK" in dockerfile

    health_block = re.search(
        r"location = /healthz\s*\{(?P<body>.*?)^\s*\}",
        nginx,
        re.MULTILINE | re.DOTALL,
    )
    api_block = re.search(
        r"location /api/\s*\{(?P<body>.*?)^\s*\}",
        nginx,
        re.MULTILINE | re.DOTALL,
    )
    spa_block = re.search(
        r"location /\s*\{(?P<body>.*?)^\s*\}",
        nginx,
        re.MULTILINE | re.DOTALL,
    )
    assert health_block is not None
    assert re.search(r'^\s*return 200 "ok\\n";$', health_block["body"], re.MULTILINE)
    assert api_block is not None
    for directive in (
        "proxy_pass http://api:8000;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    ):
        assert re.search(rf"^\s*{re.escape(directive)}$", api_block["body"], re.MULTILINE)
    assert spa_block is not None
    assert re.search(
        rf"^\s*{re.escape('try_files $uri $uri/ /index.html;')}$",
        spa_block["body"],
        re.MULTILINE,
    )


def test_frontend_image_excludes_local_secrets_and_build_artifacts() -> None:
    ignored = {
        line.strip()
        for line in (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "credentials*",
        "service-account*",
        "secrets/",
        "node_modules",
        "dist",
        "coverage",
        ".eslintcache",
        ".vite",
    } <= ignored


def test_nginx_applies_browser_security_headers_to_every_status() -> None:
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    headers = dict(
        re.findall(
            r"^\s*add_header\s+([A-Za-z-]+)\s+(.+?)\s+always;$",
            nginx,
            re.MULTILINE,
        )
    )

    csp = headers["Content-Security-Policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert headers["X-Frame-Options"] == "DENY"


def test_mattermost_is_opt_in_and_persistent() -> None:
    compose = _compose()
    services = compose["services"]

    assert services["mattermost"]["profiles"] == ["mattermost"]
    assert services["mattermost-db"]["profiles"] == ["mattermost"]
    assert services["mattermost-db"]["image"] == "postgres:16-alpine"
    assert services["mattermost"]["image"] == "mattermost/mattermost-team-edition:10.5"
    assert services["mattermost"]["ports"] == ["127.0.0.1:${MATTERMOST_PORT:-8065}:8065"]
    assert services["mattermost"]["depends_on"]["mattermost-db"]["condition"] == "service_healthy"

    environment = services["mattermost"]["environment"]
    assert environment["MM_SQLSETTINGS_DRIVERNAME"] == "postgres"
    assert "mattermost-db:5432" in environment["MM_SQLSETTINGS_DATASOURCE"]
    assert (
        environment["MM_SERVICESETTINGS_SITEURL"] == "${MATTERMOST_SITEURL:-http://localhost:8065}"
    )
    assert environment["MM_SERVICESETTINGS_ENABLELOCALMODE"] == "true"
    assert environment["MM_SERVICESETTINGS_ENABLEDEVELOPER"] == "true"
    assert environment["MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS"] == "true"

    assert {"postgres-data", "mattermost-db-data", "mattermost-data", "mattermost-config"} <= set(
        compose["volumes"]
    )


def test_mattermost_password_has_one_validated_raw_and_dsn_value() -> None:
    compose = _compose()
    services = compose["services"]
    password_expression = f"${{MATTERMOST_DB_PASSWORD:-{MATTERMOST_LOCAL_PASSWORD}}}"

    assert services["mattermost-config-check"]["profiles"] == ["mattermost"]
    assert services["mattermost-config-check"]["environment"]["MATTERMOST_DB_PASSWORD"] == (
        password_expression
    )
    assert services["mattermost-db"]["environment"]["POSTGRES_PASSWORD"] == password_expression
    assert password_expression in services["mattermost"]["environment"]["MM_SQLSETTINGS_DATASOURCE"]
    assert (
        services["mattermost-db"]["depends_on"]["mattermost-config-check"]["condition"]
        == "service_completed_successfully"
    )

    script_path = ROOT / "scripts" / "validate_mattermost_password.sh"
    safe = subprocess.run(
        ["/bin/sh", str(script_path)],
        env={**os.environ, "MATTERMOST_DB_PASSWORD": MATTERMOST_LOCAL_PASSWORD},
        capture_output=True,
        text=True,
        check=False,
    )
    assert safe.returncode == 0
    for unsafe in ("", "short", "has space 123456", "colon:12345678901", "slash/12345678901"):
        rejected = subprocess.run(
            ["/bin/sh", str(script_path)],
            env={**os.environ, "MATTERMOST_DB_PASSWORD": unsafe},
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode != 0
        if unsafe:
            assert unsafe not in rejected.stderr


def test_host_ports_and_mattermost_password_contract_are_documented() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "WEB_PORT=8080" in example
    assert "MATTERMOST_PORT=8065" in example
    assert f"MATTERMOST_DB_PASSWORD={MATTERMOST_LOCAL_PASSWORD}" in example
    assert "[A-Za-z0-9._~-]" in example
    assert "rejected before Mattermost starts" in example
