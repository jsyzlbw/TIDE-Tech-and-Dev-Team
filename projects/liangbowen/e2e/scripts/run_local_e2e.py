#!/usr/bin/env python3
"""Run Playwright against a bounded, disposable local application stack."""

from __future__ import annotations

import atexit
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO

E2E_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = E2E_DIR.parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
VENV_DIR = ROOT_DIR / ".venv"
MATTERMOST_COMMAND_TOKEN = "e2e-command-token-26chars"
MATTERMOST_DEMO_SETUP_KEY = "e2e-demo-setup-key-26chars"
COMMAND_TIMEOUT_SECONDS = 120.0
POSTGRES_STOP_TIMEOUT_SECONDS = 30.0
PLAYWRIGHT_TIMEOUT_SECONDS = 240.0
PROCESS_INSPECTION_TIMEOUT_SECONDS = 2.0
PORT_RESERVATION_ATTEMPTS = 16
SERVICE_START_ATTEMPTS = 3
PORT_CONFLICT_MARKERS = (
    "address already in use",
    "eaddrinuse",
)


class StackError(RuntimeError):
    """Raised when the disposable E2E stack cannot be started safely."""


class PortConflictError(StackError):
    """Raised only when a service log proves that a selected port was occupied."""


def executable(name: str, *, fallback: Path | None = None) -> str:
    resolved = shutil.which(name)
    if resolved is not None:
        return resolved
    if fallback is not None and fallback.is_file():
        return str(fallback)
    raise StackError(f"required executable is unavailable: {name}")


def reserve_port(
    excluded: set[int] | None = None,
    *,
    attempts: int = PORT_RESERVATION_ATTEMPTS,
) -> int:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    unavailable = excluded or set()
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in unavailable:
            return port
    raise StackError("could not reserve a distinct local port")


def is_explicit_port_conflict(output: str, *, port: int | None = None) -> bool:
    normalized = output.casefold()
    if any(marker in normalized for marker in PORT_CONFLICT_MARKERS):
        return True
    return port is not None and f"port {port} is already in use" in normalized


def read_log_segment(path: Path, offset: int) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def remove_owned_temporary_directory(path: Path) -> None:
    resolved_temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    resolved = path.resolve(strict=True)
    if (
        not resolved.is_relative_to(resolved_temp_root)
        or resolved == resolved_temp_root
        or not resolved.name.startswith("ai-grading-e2e-")
    ):
        raise StackError("refusing to remove an unowned temporary directory")
    shutil.rmtree(resolved)


def wait_managed(
    process: subprocess.Popen[bytes],
    name: str,
    *,
    timeout: float,
) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_group(process, name)
        raise StackError(f"{name} timed out after {timeout:g} seconds") from None
    except BaseException:
        terminate_group(process, name)
        raise


def run_checked(
    command: list[str],
    *,
    cwd: Path = ROOT_DIR,
    env: dict[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    name: str | None = None,
) -> None:
    phase = name or Path(command[0]).name
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    return_code = wait_managed(process, phase, timeout=timeout)
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def wait_for_http(
    url: str,
    process: subprocess.Popen[bytes],
    *,
    port: int,
    lsof: str,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise StackError(
                f"process {process.pid} exited before readiness (code {exit_code})"
            )
        try:
            with local_opener.open(url, timeout=1.0) as response:
                if response.status == 200 and listener_owned_by_process_group(
                    lsof,
                    port,
                    process,
                ):
                    return
                last_error = StackError("readiness responder is not owned by this task")
        except urllib.error.HTTPError as error:
            try:
                body = error.read(512).decode("utf-8", errors="replace")
            except OSError:
                body = ""
            last_error = StackError(f"HTTP {error.code}: {body}")
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.15)
    detail = str(last_error) if last_error is not None else "unknown"
    raise StackError(f"timed out waiting for {url} ({detail})")


def terminate_group(process: subprocess.Popen[bytes] | None, name: str) -> None:
    if process is None:
        return
    process_exited = process.poll() is not None
    try:
        process_group = process.pid if process_exited else os.getpgid(process.pid)
    except ProcessLookupError:
        process_group = process.pid
    print(f"e2e cleanup: stopping {name} pid={process.pid} pgid={process_group}")
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    if process_exited:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=3)


def listener_owned_by_process_group(
    lsof: str,
    port: int,
    process: subprocess.Popen[bytes],
) -> bool:
    if process.poll() is not None:
        return False
    try:
        expected_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return False
    inspector = subprocess.Popen(
        [
            lsof,
            "-nP",
            "-a",
            "-t",
            f"-iTCP@127.0.0.1:{port}",
            "-sTCP:LISTEN",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = inspector.communicate(timeout=PROCESS_INSPECTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        terminate_group(inspector, "listener ownership inspection")
        return False
    except BaseException:
        terminate_group(inspector, "listener ownership inspection")
        raise
    if inspector.returncode != 0:
        return False
    for raw_pid in stdout.splitlines():
        if not raw_pid.isdigit():
            continue
        try:
            if os.getpgid(int(raw_pid)) == expected_group:
                return True
        except ProcessLookupError:
            continue
    return False


def read_process_command(pid: int) -> str | None:
    process = subprocess.Popen(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=PROCESS_INSPECTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        terminate_group(process, "postgres identity inspection")
        return None
    except BaseException:
        terminate_group(process, "postgres identity inspection")
        raise
    if process.returncode != 0:
        return None
    command = stdout.strip()
    return command or None


def owned_postgres_pid(
    pgdata: Path,
    temporary_root: Path,
    *,
    expected_port: int,
) -> int | None:
    try:
        resolved_root = temporary_root.resolve(strict=True)
        resolved_pgdata = pgdata.resolve(strict=True)
        if not resolved_pgdata.is_relative_to(resolved_root):
            return None
        lines = (
            (resolved_pgdata / "postmaster.pid")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if len(lines) < 4:
            return None
        pid = int(lines[0])
        recorded_pgdata = Path(lines[1]).resolve(strict=True)
        recorded_port = int(lines[3])
    except (OSError, TypeError, ValueError):
        return None
    if pid <= 1 or recorded_pgdata != resolved_pgdata or recorded_port != expected_port:
        return None
    command = read_process_command(pid)
    if command is None:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if not arguments or Path(arguments[0]).name not in {"postgres", "postmaster"}:
        return None
    try:
        data_index = arguments.index("-D")
        port_index = arguments.index("-p")
        command_pgdata = Path(arguments[data_index + 1]).resolve(strict=True)
        command_port = int(arguments[port_index + 1])
    except (IndexError, OSError, TypeError, ValueError):
        return None
    if command_pgdata != resolved_pgdata or command_port != expected_port:
        return None
    return pid


def show_log_tail(path: Path, *, lines: int = 80) -> None:
    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not content:
        return
    print(
        f"--- {path.name} (last {min(lines, len(content))} lines) ---", file=sys.stderr
    )
    print("\n".join(content[-lines:]), file=sys.stderr)


class LocalStack:
    def __init__(self) -> None:
        self.initdb = executable("initdb", fallback=Path("/opt/homebrew/bin/initdb"))
        self.pg_ctl = executable("pg_ctl", fallback=Path("/opt/homebrew/bin/pg_ctl"))
        self.createdb = executable(
            "createdb", fallback=Path("/opt/homebrew/bin/createdb")
        )
        self.python = (
            str(VENV_DIR / "bin" / "python")
            if (VENV_DIR / "bin" / "python").is_file()
            else executable("python3.12")
        )
        self.alembic = (
            str(VENV_DIR / "bin" / "alembic")
            if (VENV_DIR / "bin" / "alembic").is_file()
            else executable("alembic")
        )
        self.npm = executable("npm")
        self.lsof = executable("lsof", fallback=Path("/usr/sbin/lsof"))

        self.temp_path = Path(tempfile.mkdtemp(prefix="ai-grading-e2e-"))
        self.pgdata = self.temp_path / "postgres"
        self.pglog = self.temp_path / "postgres.log"
        self.api_log = self.temp_path / "api.log"
        self.web_log = self.temp_path / "web.log"
        self.api_process: subprocess.Popen[bytes] | None = None
        self.web_process: subprocess.Popen[bytes] | None = None
        self.test_process: subprocess.Popen[bytes] | None = None
        self.api_log_handle: IO[bytes] | None = None
        self.web_log_handle: IO[bytes] | None = None
        self.pg_start_attempted = False
        self.cleaned = False
        self.cleaning = False
        self.cleanup_errors: list[tuple[str, BaseException]] = []
        self.pg_port: int | None = None
        self.api_port: int | None = None
        self.web_port: int | None = None
        self.database_url: str | None = None

    @staticmethod
    def _required(value: int | str | None, name: str) -> int | str:
        if value is None:
            raise StackError(f"{name} is not initialized")
        return value

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("FORCE_COLOR", None)
        env.pop("NO_COLOR", None)
        database_url = str(self._required(self.database_url, "database URL"))
        cors_origins = (
            [] if self.web_port is None else [f"http://127.0.0.1:{self.web_port}"]
        )
        env.update(
            {
                "APP_ENV": "test",
                "DATABASE_URL": database_url,
                "JWT_SECRET": "e2e-local-jwt-secret-never-share",
                "AGENT_PROVIDER": "mock",
                "AGENT_MOCK_FIXTURE": "partial",
                "CELERY_TASK_ALWAYS_EAGER": "true",
                "EVALUATION_DISPATCH_ENABLED": "true",
                "MATTERMOST_COMMAND_TOKEN": MATTERMOST_COMMAND_TOKEN,
                "MATTERMOST_DEMO_SETUP_KEY": MATTERMOST_DEMO_SETUP_KEY,
                "CORS_ORIGINS": json.dumps(cors_origins),
                "PYTHONPATH": str(BACKEND_DIR),
                "MIGRATION_LEGACY_PROCESSES_STOPPED": "true",
                "PYTHONUNBUFFERED": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        return env

    def _start_postgres(self) -> None:
        last_conflict: PortConflictError | None = None
        for attempt in range(1, SERVICE_START_ATTEMPTS + 1):
            self.pg_port = reserve_port()
            self.database_url = (
                f"postgresql+asyncpg://postgres@127.0.0.1:{self.pg_port}/grader_e2e"
            )
            log_offset = self.pglog.stat().st_size if self.pglog.exists() else 0
            self.pg_start_attempted = True
            try:
                run_checked(
                    [
                        self.pg_ctl,
                        "-D",
                        str(self.pgdata),
                        "-l",
                        str(self.pglog),
                        "-o",
                        f"-p {self.pg_port} -h 127.0.0.1",
                        "-w",
                        "start",
                    ],
                    timeout=60,
                    name="PostgreSQL startup",
                )
                return
            except (StackError, subprocess.CalledProcessError) as error:
                output = read_log_segment(self.pglog, log_offset)
                if not is_explicit_port_conflict(output, port=self.pg_port):
                    raise
                last_conflict = PortConflictError(
                    f"PostgreSQL port was occupied on attempt {attempt}"
                )
                if (self.pgdata / "postmaster.pid").is_file():
                    self._stop_postgres()
                self.pg_start_attempted = False
                if attempt == SERVICE_START_ATTEMPTS:
                    raise last_conflict from error
        raise last_conflict or StackError("PostgreSQL startup did not run")

    def start(self) -> None:
        run_checked(
            [
                self.initdb,
                "-D",
                str(self.pgdata),
                "-U",
                "postgres",
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            name="PostgreSQL initialization",
        )
        self._start_postgres()
        env = self.environment()
        postmaster_pid = (self.pgdata / "postmaster.pid").read_text().splitlines()[0]
        print(f"e2e stack: postgres pid={postmaster_pid} port={self.pg_port}")
        run_checked(
            [
                self.createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(self.pg_port),
                "-U",
                "postgres",
                "grader_e2e",
            ],
            name="E2E database creation",
        )
        run_checked(
            [self.alembic, "-c", str(BACKEND_DIR / "alembic.ini"), "upgrade", "head"],
            env=env,
            name="database migration",
        )
        run_checked(
            [self.python, "-m", "scripts.seed_demo"],
            cwd=BACKEND_DIR,
            env=env,
            name="acceptance seed",
        )

        self._start_http_services()

    def _api_command(self) -> list[str]:
        return [
            self.python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.api_port),
        ]

    def _web_command(self) -> list[str]:
        return [
            self.npm,
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.web_port),
            "--strictPort",
        ]

    def _start_http_pair_attempt(self) -> None:
        env = self.environment()
        api_log_offset = self.api_log.stat().st_size if self.api_log.exists() else 0
        self.api_log_handle = self.api_log.open("ab")
        self.api_process = subprocess.Popen(
            self._api_command(),
            cwd=BACKEND_DIR,
            env=env,
            stdout=self.api_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"e2e stack: api pid={self.api_process.pid} port={self.api_port}")
        try:
            wait_for_http(
                f"http://127.0.0.1:{self.api_port}/health/ready",
                self.api_process,
                port=int(self._required(self.api_port, "API port")),
                lsof=self.lsof,
            )
        except StackError as error:
            exited = self._settled_exit(self.api_process)
            self.api_log_handle.flush()
            output = read_log_segment(self.api_log, api_log_offset)
            if exited and is_explicit_port_conflict(output, port=self.api_port):
                raise PortConflictError("API port was occupied") from error
            raise

        frontend_env = env.copy()
        frontend_env["VITE_API_BASE_URL"] = f"http://127.0.0.1:{self.api_port}/api/v1"
        web_log_offset = self.web_log.stat().st_size if self.web_log.exists() else 0
        self.web_log_handle = self.web_log.open("ab")
        self.web_process = subprocess.Popen(
            self._web_command(),
            cwd=FRONTEND_DIR,
            env=frontend_env,
            stdout=self.web_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"e2e stack: web pid={self.web_process.pid} port={self.web_port}")
        try:
            wait_for_http(
                f"http://127.0.0.1:{self.web_port}/login",
                self.web_process,
                port=int(self._required(self.web_port, "web port")),
                lsof=self.lsof,
            )
        except StackError as error:
            exited = self._settled_exit(self.web_process)
            self.web_log_handle.flush()
            output = read_log_segment(self.web_log, web_log_offset)
            if exited and is_explicit_port_conflict(output, port=self.web_port):
                raise PortConflictError("web port was occupied") from error
            raise

    @staticmethod
    def _settled_exit(process: subprocess.Popen[bytes]) -> bool:
        if process.poll() is not None:
            return True
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _reset_http_attempt(self) -> None:
        errors: list[BaseException] = []
        for name, process in (("web", self.web_process), ("api", self.api_process)):
            try:
                terminate_group(process, name)
            except BaseException as error:  # noqa: BLE001 - both groups must be attempted
                errors.append(error)
        for handle in (self.web_log_handle, self.api_log_handle):
            if handle is None:
                continue
            try:
                handle.close()
            except BaseException as error:  # noqa: BLE001 - both handles must be attempted
                errors.append(error)
        self.web_process = None
        self.api_process = None
        self.web_log_handle = None
        self.api_log_handle = None
        if errors:
            raise StackError(
                "failed to reset a conflicted HTTP startup attempt"
            ) from errors[0]

    def _start_http_services(self) -> None:
        pg_port = int(self._required(self.pg_port, "PostgreSQL port"))
        last_conflict: PortConflictError | None = None
        for attempt in range(1, SERVICE_START_ATTEMPTS + 1):
            self.api_port = reserve_port({pg_port})
            self.web_port = reserve_port({pg_port, self.api_port})
            try:
                self._start_http_pair_attempt()
                return
            except PortConflictError as error:
                last_conflict = error
                self._reset_http_attempt()
                if attempt == SERVICE_START_ATTEMPTS:
                    raise PortConflictError(
                        f"HTTP ports remained occupied after {attempt} attempts"
                    ) from error
        raise last_conflict or StackError("HTTP startup did not run")

    def test_environment(self) -> dict[str, str]:
        env = self.environment()
        web_port = int(self._required(self.web_port, "web port"))
        api_port = int(self._required(self.api_port, "API port"))
        env.update(
            {
                "E2E_BASE_URL": f"http://127.0.0.1:{web_port}",
                "E2E_API_BASE_URL": f"http://127.0.0.1:{api_port}/api/v1",
                "E2E_MATTERMOST_COMMAND_TOKEN": MATTERMOST_COMMAND_TOKEN,
                "E2E_MATTERMOST_DEMO_SETUP_KEY": MATTERMOST_DEMO_SETUP_KEY,
            }
        )
        return env

    def _stop_postgres(self) -> None:
        if not self.pg_start_attempted:
            return
        try:
            run_checked(
                [
                    self.pg_ctl,
                    "-D",
                    str(self.pgdata),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                timeout=POSTGRES_STOP_TIMEOUT_SECONDS,
                name="PostgreSQL shutdown",
            )
            return
        except BaseException as stop_error:
            if not (self.pgdata / "postmaster.pid").is_file():
                raise StackError(
                    "PostgreSQL shutdown failed without proof that the server stopped"
                ) from stop_error
            pg_port = int(self._required(self.pg_port, "PostgreSQL port"))
            postmaster_pid = owned_postgres_pid(
                self.pgdata,
                self.temp_path,
                expected_port=pg_port,
            )
            if postmaster_pid is None:
                raise StackError(
                    "PostgreSQL shutdown failed and the remaining PID identity was not owned"
                ) from stop_error

            print(
                "e2e cleanup: pg_ctl failed; terminating verified postgres "
                f"pid={postmaster_pid}"
            )
            try:
                os.kill(postmaster_pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            for _ in range(50):
                try:
                    os.kill(postmaster_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)

            still_owned = owned_postgres_pid(
                self.pgdata,
                self.temp_path,
                expected_port=pg_port,
            )
            if still_owned != postmaster_pid:
                raise StackError(
                    "PostgreSQL PID identity changed before forced termination"
                ) from stop_error
            print(f"e2e cleanup: forcing verified postgres pid={postmaster_pid}")
            try:
                os.kill(postmaster_pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    def cleanup(self) -> tuple[tuple[str, BaseException], ...]:
        if self.cleaned or self.cleaning:
            return tuple(self.cleanup_errors)
        self.cleaning = True
        errors: list[tuple[str, BaseException]] = []

        def attempt(label: str, action: Callable[[], object]) -> None:
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - cleanup must continue
                errors.append((label, error))

        try:
            attempt(
                "Playwright",
                lambda: terminate_group(self.test_process, "Playwright"),
            )
            attempt("web", lambda: terminate_group(self.web_process, "web"))
            attempt("api", lambda: terminate_group(self.api_process, "api"))
            if self.web_log_handle is not None:
                attempt("web log", self.web_log_handle.close)
            if self.api_log_handle is not None:
                attempt("api log", self.api_log_handle.close)
            postgres_stopped = not self.pg_start_attempted
            if self.pg_start_attempted:
                print(f"e2e cleanup: stopping postgres data={self.pgdata}")
                try:
                    self._stop_postgres()
                except BaseException as error:  # noqa: BLE001 - retention is mandatory
                    errors.append(("PostgreSQL", error))
                else:
                    postgres_stopped = True
            if postgres_stopped:
                attempt(
                    "temporary directory",
                    lambda: remove_owned_temporary_directory(self.temp_path),
                )
            else:
                print(
                    "e2e cleanup warning: PostgreSQL stop was not confirmed; "
                    f"retaining task directory {self.temp_path}",
                    file=sys.stderr,
                )
        finally:
            self.cleaning = False
            self.cleaned = True
            self.cleanup_errors = errors
        for label, error in errors:
            print(
                f"e2e cleanup warning: {label} failed ({type(error).__name__})",
                file=sys.stderr,
            )
        return tuple(errors)


def main() -> int:
    stack = LocalStack()
    atexit.register(stack.cleanup)
    exit_code = 1

    def stop_from_signal(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, stop_from_signal)
    signal.signal(signal.SIGTERM, stop_from_signal)
    try:
        stack.start()
        stack.test_process = subprocess.Popen(
            [stack.npm, "run", "test:playwright"],
            cwd=E2E_DIR,
            env=stack.test_environment(),
            start_new_session=True,
        )
        return_code = wait_managed(
            stack.test_process,
            "Playwright acceptance suite",
            timeout=PLAYWRIGHT_TIMEOUT_SECONDS,
        )
        if return_code != 0:
            show_log_tail(stack.api_log)
            show_log_tail(stack.web_log)
        exit_code = return_code
    except (StackError, subprocess.CalledProcessError) as error:
        print(f"e2e stack failed: {error}", file=sys.stderr)
        show_log_tail(stack.pglog)
        show_log_tail(stack.api_log)
        show_log_tail(stack.web_log)
        exit_code = 1
    except KeyboardInterrupt:
        print("e2e stack interrupted", file=sys.stderr)
        exit_code = 130
    finally:
        cleanup_errors = stack.cleanup()
        atexit.unregister(stack.cleanup)
    if cleanup_errors and exit_code == 0:
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
