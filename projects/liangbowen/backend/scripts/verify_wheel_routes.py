from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_PREFIX = "ai_grading_backend-"
EXPECTED_REVIEW_PATHS = (
    "/api/v1/reports/{report_id}",
    "/api/v1/reports/{report_id}/confirm",
    "/api/v1/reports/{report_id}/reevaluate",
)
MATTERMOST_COMMAND_PATH = "/api/v1/integrations/mattermost/commands"
MATTERMOST_DEMO_PATH = "/api/v1/integrations/mattermost/demo-bindings"
MATTERMOST_ACTION_PATH = "/api/v1/integrations/mattermost/actions"


def _latest_wheel(wheel_dir: Path) -> Path:
    candidates = tuple(wheel_dir.glob(f"{PACKAGE_PREFIX}*.whl"))
    if not candidates:
        raise FileNotFoundError(f"no {PACKAGE_PREFIX} wheel found in {wheel_dir}")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def verify_installed_wheel(wheel: Path) -> None:
    if not wheel.is_file():
        raise FileNotFoundError(f"wheel not found: {wheel}")
    with tempfile.TemporaryDirectory(prefix="ai-grading-wheel-smoke-") as raw_target:
        target = Path(raw_target).resolve()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(target),
                str(wheel.resolve()),
            ],
            check=True,
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        expected = repr(EXPECTED_REVIEW_PATHS)
        smoke = f"""
import asyncio
import json
from pathlib import Path
import uuid
import httpx
import app
from pydantic import SecretStr
from app.core.config import Settings
from app.integrations.mattermost import actions as actions_module
from app.integrations.mattermost import card as card_module
from app.integrations.mattermost import client as client_module
from app.integrations.mattermost.actions import parse_and_verify_action
from app.integrations.mattermost.card import ReportCardInput, render_report_card
from app.integrations.mattermost.client import MattermostClient
from app.main import create_app

installed_root = Path.cwd().resolve()
for packaged_module in (app, actions_module, card_module, client_module):
    module_path = Path(packaged_module.__file__).resolve()
    assert module_path.is_relative_to(installed_root), (module_path, installed_root)
paths = create_app().openapi()["paths"]
expected = {expected}
missing = set(expected) - paths.keys()
assert not missing, sorted(missing)
MATTERMOST_COMMAND_PATH = "{MATTERMOST_COMMAND_PATH}"
MATTERMOST_DEMO_PATH = "{MATTERMOST_DEMO_PATH}"
MATTERMOST_ACTION_PATH = "{MATTERMOST_ACTION_PATH}"
safe_paths = create_app(Settings(
    app_env="test",
    jwt_secret="test-only",
    mattermost_command_token=SecretStr("wheel-command-placeholder"),
    mattermost_demo_setup_key=SecretStr("wheel-demo-placeholder"),
)).openapi()["paths"]
production_paths = create_app(Settings(
    app_env="production",
    jwt_secret="x" * 32,
    mattermost_command_token=SecretStr("wheel-command-placeholder"),
    mattermost_demo_setup_key=SecretStr("wheel-demo-placeholder"),
)).openapi()["paths"]
assert MATTERMOST_COMMAND_PATH in safe_paths
assert MATTERMOST_ACTION_PATH in safe_paths
assert MATTERMOST_DEMO_PATH in safe_paths
assert MATTERMOST_COMMAND_PATH in production_paths
assert MATTERMOST_ACTION_PATH in production_paths
assert MATTERMOST_DEMO_PATH not in production_paths

async def exercise_mattermost_delivery_and_action():
    requests = []
    async def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/channels/direct"):
            return httpx.Response(201, json={{"id": "0123456789abcdefghijklmnop"}})
        return httpx.Response(201, json={{"id": "1123456789abcdefghijklmnop"}})

    client = MattermostClient(
        base_url="http://mattermost.test",
        token=SecretStr("wheel-bot-placeholder"),
        bot_user_id="2123456789abcdefghijklmnop",
        allow_insecure_http=True,
        transport=httpx.MockTransport(handler),
    )
    try:
        recipient_id = "3123456789abcdefghijklmnop"
        channel_id = await client.create_direct_channel(recipient_id)
        report_id = uuid.uuid4()
        delivery_id = uuid.uuid4()
        card = render_report_card(ReportCardInput(
            outbox_id=delivery_id,
            report_id=report_id,
            recipient_user_id=recipient_id,
            channel_id=channel_id,
            assignment_code="A8",
            assignment_title="Wheel smoke",
            score=88,
            grade="B",
            completeness="complete",
            major_issues=(),
            suggestions=(),
            limitations=(),
            action_url="http://api.test/api/v1/integrations/mattermost/actions",
            console_url="http://console.test",
            action_secret=SecretStr("wheel-action-placeholder"),
            allow_insecure_http=True,
        ))
        post_id = await client.create_post(channel_id, card.message, card.props)
    finally:
        await client.aclose()
    action = card.props["attachments"][0]["actions"][0]
    context = action["integration"]["context"]
    parsed = parse_and_verify_action(
        json.dumps({{
            "user_id": recipient_id,
            "post_id": post_id,
            "channel_id": channel_id,
            "team_id": "",
            "context": context,
        }}).encode(),
        "application/json",
        SecretStr("wheel-action-placeholder"),
    )
    assert parsed.context.delivery_id == delivery_id
    assert requests[0][0].endswith("/channels/direct")
    assert requests[1][0].endswith("/posts")

asyncio.run(exercise_mattermost_delivery_and_action())
print("installed-wheel-routes:", sorted((*expected, MATTERMOST_COMMAND_PATH, MATTERMOST_ACTION_PATH)))
"""
        subprocess.run(
            [sys.executable, "-c", smoke],
            cwd=target,
            env=environment,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the newest backend wheel and verify packaged API routes."
    )
    parser.add_argument("--wheel-dir", type=Path, required=True)
    arguments = parser.parse_args()
    verify_installed_wheel(_latest_wheel(arguments.wheel_dir.resolve()))


if __name__ == "__main__":
    main()
