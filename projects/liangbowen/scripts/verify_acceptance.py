#!/usr/bin/env python3
"""Run every acceptance gate and write a small, auditable evidence manifest."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Self
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "acceptance" / "manifest.json"
LOG_DIRECTORY = ROOT / "artifacts" / "acceptance" / "logs"
MAX_LOG_BYTES = 256 * 1024
DEFAULT_STEP_TIMEOUT = 10 * 60.0
DEFAULT_TOTAL_TIMEOUT = 40 * 60.0
PROCESS_STOP_TIMEOUT = 8.0
PROCESS_TERM_GRACE = 1.0
PROCESS_MARKER_ENV = "AI_GRADING_VERIFY_PROCESS_TOKEN"
DEFAULT_CLEANUP_GRACE = 10.0

REQUIRED_SCENARIOS = [
    "complete",
    "partial",
    "incorrect",
    "ambiguous",
    "malformed_agent_output",
    "capability_boundary",
]
CAPABILITY_BOUNDARY_CHECKS = [
    "code_not_executed",
    "missing_rubric_low_confidence",
    "overlength_rejected",
    "provider_outage_preserves_submission",
    "ambiguous_prompt_human_review",
    "prompt_injection_contained",
]
EXPECTED_PLAYWRIGHT_MANIFEST = {
    "e2e/tests/account-management.spec.ts": 2,
    "e2e/tests/full-flow.spec.ts": 2,
    "e2e/tests/mattermost-adapter.spec.ts": 1,
    "e2e/tests/seeded-reports.spec.ts": 1,
}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PLAYWRIGHT_EXECUTION_LINE = re.compile(
    r"^\s*✓\s+\d+\s+(?P<path>tests/[^\s:]+\.spec\.ts):\d+:\d+\s+›",
    re.MULTILINE,
)
_PLAYWRIGHT_PASS_SUMMARY = re.compile(
    r"^\s*(?P<count>\d+) passed \([^\r\n]+\)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class CheckSpec:
    command: tuple[str, ...]
    timeout: float = DEFAULT_STEP_TIMEOUT
    requires: tuple[str, ...] = ()
    required_paths: tuple[Path, ...] = ()
    install_hint: str | None = None
    uses_postgres: bool = False


@dataclass(frozen=True)
class CommandResult:
    status: str
    returncode: int | None
    output: str
    duration_seconds: float
    reason: str | None = None


class VerificationBusy(RuntimeError):
    """Raised when another verifier owns the repository evidence lock."""


class ManifestPersistenceError(RuntimeError):
    def __init__(self, failures: Mapping[str, BaseException]) -> None:
        self.failures = dict(failures)
        targets = ", ".join(sorted(failures))
        super().__init__(f"acceptance manifest persistence failed for: {targets}")


class Deadline:
    def __init__(
        self,
        total_timeout: float,
        *,
        cleanup_grace: float = DEFAULT_CLEANUP_GRACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if total_timeout <= 0 or cleanup_grace <= 0:
            raise ValueError("deadline and cleanup grace must be positive")
        self._clock = clock
        self.cleanup_grace = cleanup_grace
        self.started = clock()
        self.ends_at = self.started + total_timeout
        self.cleanup_deadline = self.ends_at + cleanup_grace

    def remaining(self) -> float:
        return max(0.0, self.ends_at - self._clock())

    def bounded(self, phase_limit: float) -> float:
        if phase_limit <= 0:
            raise ValueError("phase limit must be positive")
        return min(phase_limit, self.remaining())

    def cleanup_cutoff(self) -> float:
        """Return the one bounded cleanup window available from this instant."""
        return min(self.cleanup_deadline, self._clock() + self.cleanup_grace)


class RepositoryRunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise VerificationBusy(
                "acceptance verification lock is unavailable"
            ) from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise VerificationBusy(
                "acceptance verification is already running for this repository"
            ) from error
        self.descriptor = descriptor
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.descriptor is None:
            return
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = None


CHECKS: dict[str, CheckSpec] = {
    "backend_unit": CheckSpec(
        (
            ".venv/bin/pytest",
            "backend/tests",
            "scripts/tests",
            "--ignore=backend/tests/acceptance",
            "-q",
        ),
        required_paths=(ROOT / ".venv" / "bin" / "pytest",),
        install_hint="make install",
        uses_postgres=True,
    ),
    "backend_acceptance": CheckSpec(
        (".venv/bin/pytest", "backend/tests/acceptance", "-q"),
        required_paths=(ROOT / ".venv" / "bin" / "pytest",),
        install_hint="make install",
        uses_postgres=True,
    ),
    "frontend_unit": CheckSpec(
        ("npm", "--prefix", "frontend", "test"),
        required_paths=(
            ROOT / "frontend" / "package-lock.json",
            ROOT / "frontend" / "node_modules" / ".package-lock.json",
        ),
        install_hint="make install-frontend",
    ),
    "frontend_build": CheckSpec(
        ("npm", "--prefix", "frontend", "run", "build"),
        required_paths=(
            ROOT / "frontend" / "package-lock.json",
            ROOT / "frontend" / "node_modules" / ".package-lock.json",
        ),
        install_hint="make install-frontend",
    ),
    "compose_config": CheckSpec(
        ("docker", "compose", "-f", "docker-compose.yml", "config", "--quiet"),
        timeout=120,
    ),
    "api_ready": CheckSpec(
        (
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            (
                "import urllib.request; "
                "urllib.request.build_opener(urllib.request.ProxyHandler({}))"
                ".open('http://127.0.0.1:8000/health/ready', timeout=3).read()"
            ),
        ),
        timeout=30,
        requires=("compose_config",),
    ),
    "web_ready": CheckSpec(
        (
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "exec",
            "-T",
            "web",
            "wget",
            "-qO-",
            "http://127.0.0.1/healthz",
        ),
        timeout=30,
        requires=("compose_config",),
    ),
    "e2e": CheckSpec(
        ("make", "e2e"),
        timeout=8 * 60,
        required_paths=(
            ROOT / "e2e" / "package-lock.json",
            ROOT / "e2e" / "node_modules" / ".package-lock.json",
        ),
        install_hint="make e2e-install",
    ),
}

LOCAL_ONLY_CHECKS = {
    "backend_unit",
    "backend_acceptance",
    "frontend_unit",
    "frontend_build",
    "e2e",
}

_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![\w-])
        (?:export[ \t]+)?
        (?P<key_quote>["']?)
        (?P<key>(?:--)?[a-z][a-z0-9_-]*)
        (?P=key_quote)
        [ \t]*[:=][ \t]*
    )
    (?P<value>
        "(?:\\.|[^"\\\r\n])*"
        | '(?:\\.|[^'\\\r\n])*'
        | [^\s,;\]\}&#]+
    )
    """
)
_AUTHORIZATION = re.compile(
    r"(?i)((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?"
    r"(?:bearer|basic)\s+[\"']?)([^\s,;\"']+)"
)
_URL_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")
_URL_QUERY_VALUE = re.compile(
    r"(?i)(?P<prefix>[?&](?P<key>[a-z][a-z0-9_-]*)=)(?P<value>[^&#\s\"']*)"
)
_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "setup_key",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "secret",
    "secret_access_key",
)
_SENSITIVE_EXACT_KEYS = {"pgpassword"}
_NON_SECRET_KEYS = {
    "status_token",
    "token_budget",
    "token_count",
    "token_limit",
    "token_usage",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def platform_metadata() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def current_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    value = completed.stdout.strip()
    return (
        value
        if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value)
        else "unknown"
    )


def _stable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_manifest(
    started_at: str,
    *,
    run_id: str = "unscoped",
    commit: str | None = None,
    platform_info: Mapping[str, str] | None = None,
    checks: Mapping[str, CheckSpec] = CHECKS,
    output: Path = OUTPUT,
) -> dict[str, Any]:
    run_directory = output.parent / "runs" / run_id
    log_directory = run_directory / "logs"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": None,
        "commit": current_commit() if commit is None else commit,
        "platform": dict(platform_info or platform_metadata()),
        "mode": "strict",
        "overall_status": "running",
        "evidence_manifest": _stable_path(run_directory / "manifest.json"),
        "checks": {
            name: {
                "command": sanitize_command(spec.command),
                "status": "pending",
                "returncode": None,
                "duration_seconds": None,
                "evidence_log": _stable_path(log_directory / f"{name}.log"),
            }
            for name, spec in checks.items()
        },
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "capability_boundary_checks": list(CAPABILITY_BOUNDARY_CHECKS),
        "expected_playwright_manifest": dict(EXPECTED_PLAYWRIGHT_MANIFEST),
        "expected_test_counts": {
            "playwright": sum(EXPECTED_PLAYWRIGHT_MANIFEST.values()),
        },
    }


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def enforce_playwright_manifest(
    result: CommandResult,
    *,
    root: Path = ROOT,
    expected: Mapping[str, int] = EXPECTED_PLAYWRIGHT_MANIFEST,
) -> CommandResult:
    """Fail a nominally passing E2E gate unless its Playwright evidence is exact."""
    if result.status != "passed":
        return result

    expected_manifest = dict(expected)
    discovered = {
        path.relative_to(root).as_posix()
        for path in (root / "e2e" / "tests").glob("*.spec.ts")
        if path.is_file()
    }
    plain_output = _ANSI_ESCAPE.sub("", result.output)
    executed = Counter(
        f"e2e/{match.group('path')}"
        for match in _PLAYWRIGHT_EXECUTION_LINE.finditer(plain_output)
    )
    summaries = [
        int(match.group("count"))
        for match in _PLAYWRIGHT_PASS_SUMMARY.finditer(plain_output)
    ]
    expected_total = sum(expected_manifest.values())
    summary_total = summaries[-1] if summaries else None
    matches = (
        discovered == set(expected_manifest)
        and dict(executed) == expected_manifest
        and sum(executed.values()) == expected_total
        and summary_total == expected_total
    )
    if matches:
        return result

    detail = (
        "Playwright manifest evidence mismatch: "
        f"expected {expected_total} tests in {sorted(expected_manifest)}; "
        f"discovered {sorted(discovered)}; "
        f"executed {sum(executed.values())} tests as {dict(sorted(executed.items()))}; "
        f"reported passed total {summary_total!r}"
    )
    output = f"{result.output.rstrip()}\n\n{detail}\n"
    return CommandResult(
        "failed",
        1,
        output,
        result.duration_seconds,
        "Playwright manifest evidence mismatch",
    )


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    marker = b"\n... <log truncated; middle omitted> ...\n"
    available = max(0, limit - len(marker))
    head_size = available // 2
    tail_size = available - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    candidate = head + marker.decode() + tail
    while len(candidate.encode("utf-8")) > limit:
        tail = tail[1:]
        candidate = head + marker.decode() + tail
    return candidate


def sanitize_log(value: str) -> str:
    safe = _AUTHORIZATION.sub(r"\1<redacted>", value)
    safe = _URL_QUERY_VALUE.sub(_redact_url_query, safe)
    safe = _SECRET_ASSIGNMENT.sub(_redact_secret_assignment, safe)
    safe = _URL_CREDENTIAL.sub(r"\1<redacted>\3", safe)
    return _truncate_utf8(safe, MAX_LOG_BYTES)


def _normalize_key(value: str) -> str:
    return value.strip("\"'").removeprefix("--").replace("-", "_").casefold()


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalize_key(value)
    if normalized in _NON_SECRET_KEYS:
        return False
    if normalized in {"authorization", "proxy_authorization"}:
        return True
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _redact_secret_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}<redacted>"


def _redact_url_query(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}<redacted>"


def sanitize_command(command: Sequence[str]) -> list[str]:
    safe: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            safe.append("<redacted>")
            redact_next = False
            continue
        safe.append(sanitize_log(argument))
        if (
            argument.startswith("--")
            and "=" not in argument
            and ":" not in argument
            and _is_sensitive_key(argument)
        ):
            redact_next = True
    return safe


def atomic_write_log(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = sanitize_log(value)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(safe)
            if safe and not safe.endswith("\n"):
                stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except (PermissionError, ProcessLookupError):
        return False


def _wait_for_group_exit(
    process_group: int,
    deadline: float,
    leader: subprocess.Popen[bytes] | subprocess.Popen[str],
) -> bool:
    leader.poll()
    while _group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        leader.poll()
    return not _group_exists(process_group)


def terminate_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str],
    *,
    process_group: int | None = None,
    cleanup_deadline: float | None = None,
) -> None:
    owned_group = process.pid if process_group is None else process_group
    deadline = (
        time.monotonic() + PROCESS_STOP_TIMEOUT
        if cleanup_deadline is None
        else cleanup_deadline
    )
    if owned_group != process.pid:
        raise RuntimeError("refusing to stop a process group not owned by this runner")

    if _group_exists(owned_group):
        try:
            os.killpg(owned_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        term_deadline = min(deadline, time.monotonic() + PROCESS_TERM_GRACE)
        if not _wait_for_group_exit(owned_group, term_deadline, process):
            try:
                os.killpg(owned_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not _wait_for_group_exit(owned_group, deadline, process):
                raise RuntimeError(
                    "owned process group did not stop before cleanup deadline"
                )

    remaining = max(0.0, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "owned process leader did not reap before cleanup deadline"
        ) from error


def _tagged_process_ids(token: str, *, deadline: float) -> set[int]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return set()
    command = (
        ["/bin/ps", "eww", "-ax", "-o", "pid=,command="]
        if sys.platform == "darwin"
        else ["ps", "eww", "-eo", "pid=,args="]
    )
    with tempfile.TemporaryFile(mode="w+b") as spool:
        inspector = subprocess.Popen(
            command,
            stdout=spool,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            inspector.wait(timeout=min(1.0, remaining))
        except subprocess.TimeoutExpired:
            terminate_process_group(
                inspector,
                process_group=inspector.pid,
                cleanup_deadline=deadline,
            )
            return set()
        if inspector.returncode != 0:
            return set()
        if os.fstat(spool.fileno()).st_size > 16 * 1024 * 1024:
            raise RuntimeError(
                "process ownership inspection output exceeded safety limit"
            )
        spool.seek(0)
        marker = f"{PROCESS_MARKER_ENV}={token}"
        found: set[int] = set()
        for line in spool.read().decode("utf-8", errors="replace").splitlines():
            if marker not in line:
                continue
            raw_pid = line.strip().split(maxsplit=1)[0]
            if raw_pid.isdigit() and int(raw_pid) != os.getpid():
                found.add(int(raw_pid))
        return found


def terminate_tagged_processes(token: str, *, cleanup_deadline: float) -> None:
    pids = _tagged_process_ids(token, deadline=cleanup_deadline)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    term_deadline = min(cleanup_deadline, time.monotonic() + PROCESS_TERM_GRACE)
    while pids and time.monotonic() < term_deadline:
        time.sleep(0.02)
        pids = _tagged_process_ids(token, deadline=term_deadline)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    while pids and time.monotonic() < cleanup_deadline:
        time.sleep(0.02)
        pids = _tagged_process_ids(token, deadline=cleanup_deadline)
    if pids:
        raise RuntimeError(
            "detached owned processes did not stop before cleanup deadline"
        )


def _read_spooled_output(spool: IO[bytes]) -> str:
    spool.flush()
    metadata = os.fstat(spool.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("command spool is not a regular file")
    if metadata.st_size > MAX_LOG_BYTES:
        return (
            f"command output omitted because raw spool exceeded {MAX_LOG_BYTES} bytes "
            f"({metadata.st_size} bytes produced)"
        )
    spool.seek(0)
    return sanitize_log(spool.read(MAX_LOG_BYTES + 1).decode("utf-8", errors="replace"))


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    cleanup_deadline: float | None = None,
) -> CommandResult:
    started = time.monotonic()
    process_cleanup_deadline = started + timeout + PROCESS_STOP_TIMEOUT
    if cleanup_deadline is not None:
        process_cleanup_deadline = min(process_cleanup_deadline, cleanup_deadline)
    process_token = f"{os.getpid()}-{time.monotonic_ns()}"
    process_environment = dict(env)
    process_environment[PROCESS_MARKER_ENV] = process_token
    descriptor, spool_name = tempfile.mkstemp(prefix="ai-grading-verify-spool-")
    spool_path = Path(spool_name)
    os.unlink(spool_path)
    spool = os.fdopen(descriptor, "w+b", buffering=0)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=process_environment,
            stdout=spool,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError:
        spool.close()
        executable = command[0] if command else "<empty command>"
        return CommandResult(
            "blocked",
            127,
            f"required executable is unavailable: {executable}",
            round(time.monotonic() - started, 3),
            "required executable is unavailable",
        )
    except OSError as error:
        spool.close()
        return CommandResult(
            "blocked",
            126,
            f"command could not start ({type(error).__name__})",
            round(time.monotonic() - started, 3),
            "command could not start",
        )

    timed_out = False
    cleanup_attempted = False
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            cleanup_attempted = True
            terminate_process_group(
                process,
                process_group=process.pid,
                cleanup_deadline=process_cleanup_deadline,
            )
            terminate_tagged_processes(
                process_token, cleanup_deadline=process_cleanup_deadline
            )
        output = _read_spooled_output(spool)
    except BaseException:
        try:
            if not cleanup_attempted:
                terminate_process_group(
                    process,
                    process_group=process.pid,
                    cleanup_deadline=process_cleanup_deadline,
                )
                terminate_tagged_processes(
                    process_token, cleanup_deadline=process_cleanup_deadline
                )
        finally:
            spool.close()
        raise
    finally:
        if not spool.closed:
            spool.close()

    if timed_out:
        suffix = f"command timed out after {timeout:g} seconds"
        output = f"{output}\n{suffix}" if output else suffix
        return CommandResult(
            "timed_out",
            124,
            output,
            round(time.monotonic() - started, 3),
            f"step timeout after {timeout:g} seconds",
        )
    returncode = int(process.returncode)
    return CommandResult(
        "passed" if returncode == 0 else "failed",
        returncode,
        output,
        round(time.monotonic() - started, 3),
        None if returncode == 0 else f"command exited with status {returncode}",
    )


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _resolve_executable(name: str, fallback: Path) -> str:
    resolved = shutil.which(name)
    if resolved is not None:
        return resolved
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError(f"required PostgreSQL executable is unavailable: {name}")


class TemporaryPostgres:
    """One isolated PostgreSQL process owned and cleaned by this verification run."""

    def __init__(self, deadline: Deadline | None = None) -> None:
        self.deadline = deadline or Deadline(DEFAULT_TOTAL_TIMEOUT)
        self.initdb = _resolve_executable("initdb", Path("/opt/homebrew/bin/initdb"))
        self.postgres = _resolve_executable(
            "postgres", Path("/opt/homebrew/bin/postgres")
        )
        self.createdb = _resolve_executable(
            "createdb", Path("/opt/homebrew/bin/createdb")
        )
        self.pg_isready = _resolve_executable(
            "pg_isready", Path("/opt/homebrew/bin/pg_isready")
        )
        self.psql = _resolve_executable("psql", Path("/opt/homebrew/bin/psql"))
        self.temporary: Path | None = None
        self.pgdata: Path | None = None
        self.socket_dir: Path | None = None
        self.log_path: Path | None = None
        self.log_handle: IO[str] | None = None
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.nonce = uuid.uuid4().hex

    @property
    def test_database_url(self) -> str:
        if self.port is None or self.socket_dir is None:
            raise RuntimeError("temporary PostgreSQL has not started")
        query = urlencode({"host": str(self.socket_dir), "port": str(self.port)})
        return (
            "postgresql+asyncpg://postgres:"
            f"{self.nonce}@/grader_acceptance_test?{query}"
        )

    def _phase_timeout(self, phase_limit: float, label: str) -> float:
        timeout = self.deadline.bounded(phase_limit)
        if timeout <= 0:
            raise TimeoutError(f"total timeout exhausted before {label}")
        return timeout

    def _checked(self, command: Sequence[str], timeout: float, label: str) -> str:
        result = run_command(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            timeout=self._phase_timeout(timeout, label),
            cleanup_deadline=self.deadline.cleanup_deadline,
        )
        if result.status != "passed":
            raise RuntimeError(f"{label} failed: {result.output[-1000:]}")
        return result.output

    def _server_command(self) -> tuple[str, ...]:
        if self.pgdata is None or self.socket_dir is None or self.port is None:
            raise RuntimeError("temporary PostgreSQL paths are not initialized")
        return (
            self.postgres,
            "-D",
            str(self.pgdata),
            "-p",
            str(self.port),
            "-h",
            "",
            "-k",
            str(self.socket_dir),
            "-c",
            f"ai_grading.verification_nonce={self.nonce}",
        )

    def _verify_ownership_and_create_database(self) -> None:
        if self.pgdata is None or self.socket_dir is None or self.port is None:
            raise RuntimeError("temporary PostgreSQL paths are not initialized")
        identity = self._checked(
            (
                self.psql,
                "-h",
                str(self.socket_dir),
                "-p",
                str(self.port),
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-AtX",
                "-c",
                (
                    "SELECT current_setting('data_directory'), "
                    "current_setting('ai_grading.verification_nonce', true)"
                ),
            ),
            5,
            "temporary PostgreSQL ownership check",
        ).strip()
        fields = identity.split("|", 1)
        if len(fields) != 2:
            # Kept for simple test doubles and clearer diagnostics.
            fields = identity.splitlines()
        actual_data = Path(fields[0]).resolve() if len(fields) == 2 else None
        expected_data = self.pgdata.resolve()
        actual_nonce = fields[1].strip() if len(fields) == 2 else ""
        if actual_data != expected_data or actual_nonce != self.nonce:
            raise RuntimeError(
                "temporary PostgreSQL ownership check failed; refusing createdb"
            )
        self._checked(
            (
                self.createdb,
                "-h",
                str(self.socket_dir),
                "-p",
                str(self.port),
                "-U",
                "postgres",
                "grader_acceptance_test",
            ),
            20,
            "test database creation",
        )

    def __enter__(self) -> Self:
        self.temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-"))
        self.pgdata = self.temporary / "postgres"
        self.socket_dir = self.temporary / "socket"
        self.socket_dir.mkdir(mode=0o700)
        self.log_path = self.temporary / "postgres.log"
        try:
            self._checked(
                (
                    self.initdb,
                    "-D",
                    str(self.pgdata),
                    "-U",
                    "postgres",
                    "-A",
                    "trust",
                    "--no-locale",
                    "--encoding=UTF8",
                ),
                60,
                "PostgreSQL initialization",
            )
            self.port = _reserve_port()
            self.log_handle = self.log_path.open("w", encoding="utf-8")
            self.process = subprocess.Popen(
                self._server_command(),
                cwd=ROOT,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            while self.deadline.remaining() > 0:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"temporary PostgreSQL exited with status {self.process.returncode}"
                    )
                probe = run_command(
                    (
                        self.pg_isready,
                        "-h",
                        str(self.socket_dir),
                        "-p",
                        str(self.port),
                        "-U",
                        "postgres",
                    ),
                    cwd=ROOT,
                    env=os.environ.copy(),
                    timeout=self._phase_timeout(2, "PostgreSQL readiness probe"),
                    cleanup_deadline=self.deadline.cleanup_deadline,
                )
                if probe.status == "passed":
                    break
                time.sleep(min(0.1, self.deadline.remaining()))
            else:
                raise TimeoutError(
                    "total timeout exhausted during PostgreSQL readiness"
                )
            self._verify_ownership_and_create_database()
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        cleanup_error: BaseException | None = None
        cleanup_cutoff = self.deadline.cleanup_cutoff()
        try:
            if self.process is not None:
                process = self.process
                terminate_process_group(
                    process,
                    process_group=process.pid,
                    cleanup_deadline=cleanup_cutoff,
                )
                self.process = None
        except BaseException as error:  # noqa: BLE001 - retain on every stop failure
            # A final exact check closes the race where the group disappears as
            # the bounded terminator reports its deadline failure.
            process.poll()
            if process.returncode is not None and not _group_exists(process.pid):
                self.process = None
            else:
                cleanup_error = error
        finally:
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
        if cleanup_error is None and self.temporary is not None:
            while True:
                try:
                    shutil.rmtree(self.temporary)
                    self.temporary = None
                    break
                except FileNotFoundError:
                    if not self.temporary.exists():
                        self.temporary = None
                        break
                except OSError as error:
                    if time.monotonic() >= cleanup_cutoff:
                        cleanup_error = RuntimeError(
                            "cleanup failed; retained owned PostgreSQL directory: "
                            f"{self.temporary} ({type(error).__name__}: {error})"
                        )
                        cleanup_error.__cause__ = error
                        break
                if time.monotonic() >= cleanup_cutoff:
                    cleanup_error = RuntimeError(
                        "cleanup failed; retained owned PostgreSQL directory: "
                        f"{self.temporary}"
                    )
                    break
                time.sleep(min(0.05, cleanup_cutoff - time.monotonic()))
        if cleanup_error is not None:
            print(
                "verification cleanup retained owned PostgreSQL directory: "
                f"{self.temporary}",
                file=sys.stderr,
                flush=True,
            )
            raise cleanup_error

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _blocked_result(reason: str) -> CommandResult:
    return CommandResult("blocked", None, reason, 0.0, reason)


def _skipped_result(reason: str) -> CommandResult:
    return CommandResult("skipped", None, reason, 0.0, reason)


def _update_entry(entry: dict[str, Any], result: CommandResult) -> None:
    entry.update(
        status=result.status,
        returncode=result.returncode,
        duration_seconds=result.duration_seconds,
    )
    if result.reason is not None:
        entry["reason"] = _truncate_utf8(sanitize_log(result.reason), 2000)


def _check_missing_paths(spec: CheckSpec) -> list[Path]:
    return [path for path in spec.required_paths if not path.exists()]


def _new_run_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def _create_run_directory(output: Path, run_id: str) -> Path:
    evidence_root = output.parent
    evidence_root.mkdir(parents=True, exist_ok=True)
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise RuntimeError("acceptance evidence root must be a real directory")
    runs = evidence_root / "runs"
    if runs.exists() and (runs.is_symlink() or not runs.is_dir()):
        raise RuntimeError("acceptance runs path must be a real directory")
    runs.mkdir(mode=0o700, exist_ok=True)
    run_directory = runs / run_id
    run_directory.mkdir(mode=0o700)
    (run_directory / "logs").mkdir(mode=0o700)
    return run_directory


def _persist_manifest(
    output: Path, run_directory: Path, manifest: Mapping[str, Any]
) -> None:
    failures = _persist_manifest_targets(output, run_directory, manifest)
    if failures:
        if any(isinstance(error, KeyboardInterrupt) for error in failures.values()):
            raise KeyboardInterrupt
        raise ManifestPersistenceError(failures)


def _persist_manifest_targets(
    output: Path,
    run_directory: Path | None,
    manifest: Mapping[str, Any],
) -> dict[str, BaseException]:
    targets = [("latest manifest", output)]
    if run_directory is not None:
        targets.insert(0, ("run manifest", run_directory / "manifest.json"))
    failures: dict[str, BaseException] = {}
    for label, path in targets:
        try:
            atomic_write_json(path, manifest)
        except BaseException as error:  # noqa: BLE001 - report each target independently
            failures[label] = error
    return failures


def _persistence_failure_summary(failures: Mapping[str, BaseException]) -> str:
    return ", ".join(
        f"{label} ({type(error).__name__})" for label, error in sorted(failures.items())
    )


def run_verification(
    *,
    checks: Mapping[str, CheckSpec] = CHECKS,
    selected: set[str] | None = None,
    output: Path = OUTPUT,
    environment: Mapping[str, str] | None = None,
    postgres_factory: Callable[[Deadline], TemporaryPostgres]
    | None = TemporaryPostgres,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    cleanup_grace: float = DEFAULT_CLEANUP_GRACE,
    mode: str = "strict",
) -> int:
    selected_names = set(checks) if selected is None else set(selected)
    unknown = selected_names - set(checks)
    if unknown:
        raise ValueError(f"unknown checks: {', '.join(sorted(unknown))}")

    lock_path = output.parent / ".verify-acceptance.lock"
    try:
        lock = RepositoryRunLock(lock_path)
        lock.__enter__()
    except VerificationBusy as error:
        print(str(error), file=sys.stderr, flush=True)
        return 75

    run_id = _new_run_id()
    run_directory: Path | None = None
    deadline = Deadline(total_timeout, cleanup_grace=cleanup_grace)
    manifest = build_manifest(now_iso(), run_id=run_id, checks=checks, output=output)
    manifest["mode"] = mode
    base_environment = dict(os.environ if environment is None else environment)
    base_environment.pop("TEST_DATABASE_URL", None)
    base_environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT / "backend"), str(ROOT / "scripts"))
            ),
            "MIGRATION_LEGACY_PROCESSES_STOPPED": "true",
        }
    )
    interrupted = False
    fatal_error: BaseException | None = None
    postgres: TemporaryPostgres | None = None
    postgres_entered = False
    postgres_error: str | None = None

    try:
        run_directory = _create_run_directory(output, run_id)
        _persist_manifest(output, run_directory, manifest)
        needs_postgres = any(
            name in selected_names
            and spec.uses_postgres
            and not _check_missing_paths(spec)
            for name, spec in checks.items()
        )
        if needs_postgres:
            if postgres_factory is None:
                postgres_error = "isolated PostgreSQL factory is unavailable"
            elif deadline.remaining() <= 0:
                postgres_error = "total timeout exhausted before PostgreSQL startup"
            else:
                try:
                    postgres = postgres_factory(deadline)
                    postgres.__enter__()
                    postgres_entered = True
                    base_environment["TEST_DATABASE_URL"] = postgres.test_database_url
                    print(
                        "verification: isolated PostgreSQL started "
                        f"pid={postgres.process.pid if postgres.process else 'unknown'} "
                        f"port={postgres.port}",
                        flush=True,
                    )
                except KeyboardInterrupt as error:
                    interrupted = True
                    fatal_error = error
                    postgres_error = (
                        "verification interrupted during PostgreSQL startup"
                    )
                except BaseException as error:  # noqa: BLE001 - persist every failure
                    postgres_error = (
                        "isolated PostgreSQL setup failed "
                        f"({type(error).__name__}): {error}"
                    )

        for name, spec in checks.items():
            entry = manifest["checks"][name]
            log_path = run_directory / "logs" / f"{name}.log"
            if name not in selected_names:
                result = _skipped_result("not selected for this verification mode")
            elif interrupted:
                result = _skipped_result("verification interrupted before this check")
            elif spec.uses_postgres and postgres_error is not None:
                result = _blocked_result(postgres_error)
            else:
                failed_dependencies = [
                    dependency
                    for dependency in spec.requires
                    if manifest["checks"][dependency]["status"] != "passed"
                ]
                missing_paths = _check_missing_paths(spec)
                remaining = deadline.remaining()
                if failed_dependencies:
                    result = _blocked_result(
                        "required check did not pass: " + ", ".join(failed_dependencies)
                    )
                elif missing_paths:
                    hint = f"; run `{spec.install_hint}`" if spec.install_hint else ""
                    result = _blocked_result(
                        "required locked dependencies are missing: "
                        + ", ".join(_stable_path(path) for path in missing_paths)
                        + hint
                    )
                elif remaining <= 0:
                    result = CommandResult(
                        "timed_out",
                        124,
                        "verification total timeout was exhausted",
                        0.0,
                        "total timeout exhausted",
                    )
                else:
                    timeout = min(spec.timeout, remaining)
                    print(f"verification: running {name}", flush=True)
                    try:
                        result = run_command(
                            spec.command,
                            cwd=ROOT,
                            env=base_environment,
                            timeout=timeout,
                            cleanup_deadline=deadline.cleanup_deadline,
                        )
                    except KeyboardInterrupt as error:
                        interrupted = True
                        fatal_error = error
                        result = CommandResult(
                            "interrupted",
                            130,
                            "verification interrupted; owned subprocess group was stopped",
                            round(time.monotonic() - deadline.started, 3),
                            "interrupted by signal or keyboard",
                        )
                    except BaseException as error:  # noqa: BLE001 - persist every failure
                        result = CommandResult(
                            "failed",
                            1,
                            f"check runner failed ({type(error).__name__}): {error}",
                            0.0,
                            "check runner raised an exception",
                        )
            if name == "e2e":
                result = enforce_playwright_manifest(result)
            atomic_write_log(log_path, result.output)
            _update_entry(entry, result)
            _persist_manifest(output, run_directory, manifest)
            print(f"verification: {name} -> {result.status}", flush=True)
    except KeyboardInterrupt as error:
        interrupted = True
        fatal_error = error
    except BaseException as error:  # noqa: BLE001 - final evidence for every failure
        fatal_error = error
        manifest["internal_error"] = sanitize_log(
            f"verification failed ({type(error).__name__})"
        )
    finally:
        if postgres is not None and postgres_entered:
            try:
                postgres.__exit__(None, None, None)
                print("verification: isolated PostgreSQL stopped", flush=True)
            except KeyboardInterrupt:
                interrupted = True
                manifest["cleanup_error"] = (
                    "temporary PostgreSQL cleanup interrupted; owned path retained"
                )
            except BaseException as error:  # noqa: BLE001 - cleanup must be recorded
                manifest["cleanup_error"] = (
                    "temporary PostgreSQL cleanup failed "
                    f"({type(error).__name__}): {sanitize_log(str(error))}"
                )

        for name, entry in manifest["checks"].items():
            if entry["status"] != "pending":
                continue
            if name not in selected_names:
                result = _skipped_result("not selected for this verification mode")
            elif interrupted:
                result = CommandResult(
                    "interrupted",
                    130,
                    "verification interrupted before this check completed",
                    0.0,
                    "interrupted before completion",
                )
            else:
                result = _skipped_result("verification stopped after an internal error")
            _update_entry(entry, result)

        manifest["finished_at"] = now_iso()
        statuses = [manifest["checks"][name]["status"] for name in selected_names]
        if interrupted:
            manifest["overall_status"] = "interrupted"
            exit_code = 130
        elif (
            fatal_error is None
            and "cleanup_error" not in manifest
            and statuses
            and all(status == "passed" for status in statuses)
        ):
            manifest["overall_status"] = "passed"
            exit_code = 0
        else:
            manifest["overall_status"] = "failed"
            exit_code = 1

        directory_failure: BaseException | None = None
        if run_directory is None:
            try:
                run_directory = _create_run_directory(output, run_id)
            except BaseException as error:  # noqa: BLE001 - report final evidence failure
                directory_failure = error
                run_directory = None
        persistence_failures: dict[str, BaseException] = {}
        persistence_interrupted = False
        if directory_failure is not None:
            persistence_failures["run directory"] = directory_failure
        for _attempt in range(2):
            attempt_failures = _persist_manifest_targets(
                output, run_directory, manifest
            )
            if any(
                isinstance(error, KeyboardInterrupt)
                for error in attempt_failures.values()
            ):
                persistence_interrupted = True
                interrupted = True
                manifest["overall_status"] = "interrupted"
                manifest["internal_error"] = (
                    "final evidence persistence was interrupted; retrying only to "
                    "record the interrupted result"
                )
                exit_code = 130
            persistence_failures = {
                **(
                    {"run directory": directory_failure}
                    if directory_failure is not None
                    else {}
                ),
                **attempt_failures,
            }
            if not persistence_failures:
                break
        if persistence_interrupted:
            print(
                "verification: final evidence persistence was interrupted; "
                "the run is interrupted even if a retry succeeded",
                file=sys.stderr,
                flush=True,
            )
        if persistence_failures:
            if not persistence_interrupted:
                manifest["overall_status"] = "failed"
                exit_code = 1
            summary = _persistence_failure_summary(persistence_failures)
            if persistence_interrupted:
                manifest["internal_error"] += (
                    "; persistence targets remain incomplete: " + summary
                )
            else:
                manifest["internal_error"] = (
                    "final evidence persistence failed; results may be incomplete: "
                    f"{summary}"
                )
            print(
                "verification: final evidence persistence failed; "
                f"results may be incomplete: {summary}",
                file=sys.stderr,
                flush=True,
            )
            # Rewrite every target once so whichever destination remains writable
            # records the non-success result and the persistence fault.
            _persist_manifest_targets(output, run_directory, manifest)
        lock.__exit__(None, None, None)
    return exit_code


@contextmanager
def _termination_as_interrupt() -> Iterator[None]:
    previous: dict[signal.Signals, Any] = {}
    interrupted = False

    def interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded acceptance checks and write an evidence manifest."
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="run all non-Docker gates; Docker/runtime checks are recorded as skipped",
    )
    parser.add_argument(
        "--step-timeout",
        type=float,
        default=None,
        help="override every per-check timeout in seconds",
    )
    parser.add_argument(
        "--total-timeout",
        type=float,
        default=DEFAULT_TOTAL_TIMEOUT,
        help="maximum wall time for PostgreSQL setup and checks",
    )
    parser.add_argument(
        "--cleanup-grace",
        type=float,
        default=DEFAULT_CLEANUP_GRACE,
        help="bounded cleanup grace after the total deadline (default: 10 seconds)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.total_timeout <= 0
        or args.cleanup_grace <= 0
        or (args.step_timeout is not None and args.step_timeout <= 0)
    ):
        print("timeouts must be positive", file=sys.stderr)
        return 2
    checks = CHECKS
    if args.step_timeout is not None:
        checks = {
            name: CheckSpec(
                spec.command,
                timeout=args.step_timeout,
                requires=spec.requires,
                required_paths=spec.required_paths,
                install_hint=spec.install_hint,
                uses_postgres=spec.uses_postgres,
            )
            for name, spec in CHECKS.items()
        }
    selected = LOCAL_ONLY_CHECKS if args.local_only else set(checks)
    mode = "local-only" if args.local_only else "strict"
    with _termination_as_interrupt():
        return run_verification(
            checks=checks,
            selected=selected,
            total_timeout=args.total_timeout,
            cleanup_grace=args.cleanup_grace,
            mode=mode,
        )


if __name__ == "__main__":
    raise SystemExit(main())
