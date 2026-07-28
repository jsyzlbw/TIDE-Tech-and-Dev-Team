from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv.parser import Binding, parse_stream

ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DotenvContractError(ValueError):
    """Raised when a dotenv file cannot safely represent a process environment."""


def _read_bindings(env_file: Path) -> list[Binding]:
    try:
        with env_file.open("r", encoding="utf-8") as stream:
            bindings = list(parse_stream(stream))
    except FileNotFoundError as exc:
        if env_file.is_symlink():
            raise DotenvContractError("dotenv file could not be read") from exc
        return []
    except (OSError, UnicodeError) as exc:
        raise DotenvContractError("dotenv file could not be read") from exc

    for binding in bindings:
        if binding.error:
            raise DotenvContractError(f"malformed dotenv file at line {binding.original.line}")
        if binding.key is not None and not ENVIRONMENT_KEY.fullmatch(binding.key):
            raise DotenvContractError(f"invalid environment key at line {binding.original.line}")
    return bindings


def merged_environment(
    env_file: Path,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(process_environment if process_environment is not None else os.environ)
    bindings = _read_bindings(env_file)
    file_values: dict[str, str] = {}
    for binding in bindings:
        if binding.key is not None:
            file_values[binding.key] = binding.value or ""
    for key, value in file_values.items():
        environment.setdefault(key, value)
    return environment


def run_with_environment(
    command: Sequence[str],
    *,
    env_file: Path,
    cwd: Path,
) -> int:
    if not command:
        raise DotenvContractError("a command is required")
    environment = merged_environment(env_file)
    completed = subprocess.run(command, cwd=cwd, env=environment, check=False)
    return completed.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute argv with safely parsed dotenv data")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        return run_with_environment(command, env_file=args.env_file, cwd=args.cwd)
    except DotenvContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"env exec failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
