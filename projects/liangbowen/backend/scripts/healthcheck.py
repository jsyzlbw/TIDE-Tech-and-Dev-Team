from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.env_exec import merged_environment

DEFAULT_API_PORT = 8000


def resolve_api_port(env_file: Path, environment: Mapping[str, str] | None = None) -> int:
    value = merged_environment(env_file, environment).get("API_PORT", str(DEFAULT_API_PORT))
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("API_PORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("API_PORT must be an integer between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check local API readiness")
    parser.add_argument("--env-file", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        port = resolve_api_port(args.env_file)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = opener.open(
            f"http://127.0.0.1:{port}/health/ready",
            timeout=5,
        )
        with response:
            print(response.read().decode())
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"health check failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
