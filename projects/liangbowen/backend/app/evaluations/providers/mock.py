import asyncio
import os
import stat
import time
from importlib.resources import files
from pathlib import Path

from app.evaluations.providers.base import (
    MAX_FIXTURE_KEY_BYTES,
    EvaluationRequest,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderResult,
    ProviderUnavailable,
    elapsed_duration_ms,
)
from app.evaluations.providers.json_safety import UnsafeJSONError, load_bounded_json
from app.evaluations.validation import normalize_evaluation

MAX_FIXTURE_BYTES = 512 * 1024


def _is_safe_fixture_key(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    try:
        if len(value.encode("utf-8")) > MAX_FIXTURE_KEY_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return all(character.isprintable() and character not in {"/", "\\"} for character in value)


class MockEvaluationProvider:
    """Read and validate deterministic local report fixtures."""

    def __init__(self, *, fixture_dir: Path | None = None) -> None:
        if fixture_dir is None:
            fixture_dir = Path(str(files("fixtures.agent_outputs")))
        if not isinstance(fixture_dir, Path):
            raise TypeError("fixture_dir must be a Path")
        try:
            root = fixture_dir.resolve(strict=True)
        except OSError as exc:
            raise ValueError("fixture_dir is unavailable") from exc
        if not root.is_dir():
            raise ValueError("fixture_dir must be a directory")
        self._fixture_dir = root
        self._cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(provider="mock", model="fixture-v1")

    async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
        if type(request) is not EvaluationRequest:
            raise TypeError("request must be an EvaluationRequest")
        try:
            request = request.revalidated()
        except (AttributeError, TypeError, ValueError, RecursionError):
            raise ProviderUnavailable(
                "evaluation request is invalid",
                code=ProviderErrorCode.CONFIGURATION,
            ) from None
        started = time.monotonic_ns()
        key = request.fixture_key
        if key is None or not _is_safe_fixture_key(key):
            raise ProviderUnavailable(
                "fixture unavailable",
                code=ProviderErrorCode.FIXTURE,
            )

        async with self._cache_lock:
            text = self._cache.get(key)
        if text is None:
            try:
                loaded_text = await asyncio.to_thread(self._load_fixture, key)
            except asyncio.CancelledError:
                raise
            except (OSError, UnicodeError, UnsafeJSONError, ValueError, RecursionError):
                raise ProviderUnavailable(
                    "fixture unavailable",
                    code=ProviderErrorCode.FIXTURE,
                ) from None
            async with self._cache_lock:
                text = self._cache.setdefault(key, loaded_text)

        duration_ms = elapsed_duration_ms(started, finished_ns=time.monotonic_ns())
        return ProviderResult(
            provider="mock",
            model="fixture-v1",
            raw_text=text,
            duration_ms=duration_ms,
        )

    def _load_fixture(self, key: str) -> str:
        if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
            raise OSError("safe fixture opening is unsupported")
        directory_flags = os.O_RDONLY
        directory_flags |= os.O_DIRECTORY
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(self._fixture_dir, directory_flags)
        try:
            file_flags = os.O_RDONLY
            file_flags |= getattr(os, "O_CLOEXEC", 0)
            file_flags |= os.O_NOFOLLOW | os.O_NONBLOCK
            file_fd = os.open(f"{key}.json", file_flags, dir_fd=directory_fd)
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_FIXTURE_BYTES:
                    raise ValueError("unsafe fixture")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(file_fd, min(64 * 1024, MAX_FIXTURE_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_FIXTURE_BYTES:
                        raise ValueError("fixture is too large")
                data = b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)

        payload = load_bounded_json(data, max_bytes=MAX_FIXTURE_BYTES)
        if type(payload) is not dict:
            raise ValueError("fixture root must be an object")
        normalize_evaluation(payload)
        return data.decode("utf-8", errors="strict")
