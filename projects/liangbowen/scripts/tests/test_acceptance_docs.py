from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _bash_blocks(markdown: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)\n```", markdown, flags=re.DOTALL)


def test_readme_separates_docker_demo_from_fresh_clone_host_e2e() -> None:
    readme = _read("README.md")

    assert "PostgreSQL 16+" in readme
    assert "Compose 固定使用 PostgreSQL 16" in readme
    for executable in ("initdb", "pg_ctl", "createdb", "lsof"):
        assert f"`{executable}`" in readme
    assert "宿主机 E2E 不使用 Docker" in readme
    assert "`make demo` 是 Docker 路径" in readme

    fresh_clone_block = next(
        block
        for block in _bash_blocks(readme)
        if "make install-frontend" in block and "make e2e-install" in block
    )
    install_frontend = fresh_clone_block.index("make install-frontend")
    install_e2e = fresh_clone_block.index("make e2e-install", install_frontend)
    run_e2e = fresh_clone_block.index("make e2e\n", install_e2e)
    assert install_frontend < install_e2e < run_e2e


def test_api_example_has_bounded_terminal_state_poll_before_reports() -> None:
    document = _read("docs/api-examples.md")
    polling_block = next(
        block for block in _bash_blocks(document) if "wait_for_job()" in block
    )

    syntax = subprocess.run(
        ["bash", "-n"],
        input=polling_block,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert "wait_seconds=120" in polling_block
    assert "request_timeout=5" in polling_block
    assert '--connect-timeout 2 --max-time "$request_timeout"' in polling_block
    assert "sleep_seconds=2" in polling_block
    assert 'sleep "$sleep_seconds"' in polling_block
    assert "succeeded)" in polling_block
    assert "failed|cancelled)" in polling_block
    assert polling_block.count("return 1") >= 4

    single_wait = document.index('wait_for_job "$JOB_ID" || exit 1')
    report_fetch = document.index("REPORTS=$(", single_wait)
    assert single_wait < report_fetch


def test_demo_script_requires_fresh_e2e_evidence_and_truthful_fallback() -> None:
    script = _read("docs/demo-script.md")

    rehearsal_block = next(
        block
        for block in _bash_blocks(script)
        if "make install-frontend" in block and "make e2e-install" in block
    )
    install_frontend = rehearsal_block.index("make install-frontend")
    install_e2e = rehearsal_block.index("make e2e-install", install_frontend)
    evidence = rehearsal_block.index(
        "make e2e && test -f artifacts/acceptance/playwright/index.html",
        install_e2e,
    )
    assert install_frontend < install_e2e < evidence
    assert "本次未复现通过" in script
    assert "不能把可能残留的旧报告说成刚刚通过" in script


def test_final_acceptance_evidence_is_redacted_and_checklist_is_honest() -> None:
    summary_text = _read("docs/evidence/clean-room-acceptance.json")
    summary = json.loads(summary_text)

    assert summary["schema_version"] == 1
    assert summary["evidence_type"] == "redacted_clean_room_acceptance_summary"
    assert re.fullmatch(r"[0-9a-f]{40}", summary["source"]["verified_commit"])
    assert summary["source"] == {
        "verified_commit": summary["source"]["verified_commit"],
        "tracked_files_only_at_start": True,
        "environment_created_from": ".env.example",
        "fresh_python_environment": True,
        "fresh_frontend_install": True,
        "fresh_e2e_install": True,
        "raw_artifacts_committed": False,
    }
    assert summary["environment"]["docker_command_available"] is False

    runs = {run["mode"]: run for run in summary["runs"]}
    assert set(runs) == {"strict", "local-only"}
    assert runs["strict"]["overall_status"] == "failed"
    assert runs["strict"]["checks"]["compose_config"] == {
        "status": "blocked",
        "returncode": 127,
        "duration_seconds": 0.002,
    }
    assert runs["local-only"]["overall_status"] == "passed"
    assert runs["local-only"]["checks"]["compose_config"]["status"] == "skipped"
    for run in runs.values():
        assert run["error_fields"] == {
            "cleanup_error_present": False,
            "internal_error_present": False,
        }
        assert (
            run["owned_process_cleanup"]["verifier_postgresql_confirmed_exited"] is True
        )
        assert (
            run["owned_process_cleanup"]["e2e_managed_processes_confirmed_stopped"]
            is True
        )
        assert run["test_totals"]["playwright"] == 4
        assert run["playwright_details"]["passed"] == 4
        assert run["playwright_details"]["duration_seconds"] > 0
        assert run["playwright_details"]["seeded_reports_spec_duration_seconds"] > 0
        assert set(run["checks"]) == {
            "backend_unit",
            "backend_acceptance",
            "frontend_unit",
            "frontend_build",
            "compose_config",
            "api_ready",
            "web_ready",
            "e2e",
        }

    forbidden = (
        "/tmp/",
        "/private/tmp/",
        "/var/folders/",
        "Teacher123!",
        "Student123!",
        "Bearer ",
        "access_token",
        "e2e-command-token",
        "e2e-demo-setup-key",
    )
    assert all(marker not in summary_text for marker in forbidden)

    checklist = _read("docs/submission-checklist.md")
    states = re.findall(
        r"^\|\s*\d+\s*\|.*?\|\s*(\[[ x]\])\s*\|", checklist, re.MULTILINE
    )
    assert len(states) == 10
    assert states.count("[x]") == 7
    assert states.count("[ ]") == 3
