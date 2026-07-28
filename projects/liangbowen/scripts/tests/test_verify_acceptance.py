from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import verify_acceptance as verifier


class ManifestContractTests(unittest.TestCase):
    def test_manifest_exposes_every_acceptance_gate_and_scenario(self) -> None:
        manifest = verifier.build_manifest(
            "2026-07-25T12:00:00+08:00",
            commit="abc123",
            platform_info={"system": "test"},
        )

        self.assertEqual(
            set(manifest["checks"]),
            {
                "backend_unit",
                "backend_acceptance",
                "frontend_unit",
                "frontend_build",
                "compose_config",
                "api_ready",
                "web_ready",
                "e2e",
            },
        )
        self.assertEqual(
            manifest["required_scenarios"],
            [
                "complete",
                "partial",
                "incorrect",
                "ambiguous",
                "malformed_agent_output",
                "capability_boundary",
            ],
        )
        self.assertEqual(
            manifest["capability_boundary_checks"],
            [
                "code_not_executed",
                "missing_rubric_low_confidence",
                "overlength_rejected",
                "provider_outage_preserves_submission",
                "ambiguous_prompt_human_review",
                "prompt_injection_contained",
            ],
        )
        self.assertEqual(
            manifest["expected_playwright_manifest"],
            {
                "e2e/tests/account-management.spec.ts": 2,
                "e2e/tests/full-flow.spec.ts": 2,
                "e2e/tests/mattermost-adapter.spec.ts": 1,
                "e2e/tests/seeded-reports.spec.ts": 1,
            },
        )
        self.assertEqual(manifest["expected_test_counts"], {"playwright": 6})
        self.assertEqual(manifest["commit"], "abc123")
        self.assertEqual(manifest["platform"], {"system": "test"})
        self.assertEqual(manifest["started_at"], "2026-07-25T12:00:00+08:00")
        self.assertIsNone(manifest["finished_at"])
        for check in manifest["checks"].values():
            self.assertEqual(check["status"], "pending")
            self.assertIn("command", check)
            self.assertIn("evidence_log", check)

    def test_run_manifest_uses_an_isolated_run_id_evidence_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            manifest = verifier.build_manifest(
                "2026-07-25T12:00:00+08:00",
                run_id="run-safe-123",
                output=output,
            )

        self.assertEqual(manifest["run_id"], "run-safe-123")
        for check in manifest["checks"].values():
            self.assertIn("runs/run-safe-123/logs/", check["evidence_log"])
        self.assertIn("runs/run-safe-123/manifest.json", manifest["evidence_manifest"])

    def test_manifest_is_written_atomically_without_temporary_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nested" / "manifest.json"
            verifier.atomic_write_json(output, {"status": "passed"})

            self.assertEqual(json.loads(output.read_text()), {"status": "passed"})
            self.assertEqual(list(output.parent.glob(".manifest.json.*.tmp")), [])

    def test_playwright_evidence_enforces_discovered_and_executed_manifest(
        self,
    ) -> None:
        expected = {
            "e2e/tests/account-management.spec.ts": 2,
            "e2e/tests/full-flow.spec.ts": 1,
        }
        output = """\
  ✓  1 tests/account-management.spec.ts:49:1 › first account flow (1.0s)
  ✓  2 tests/account-management.spec.ts:111:1 › second account flow (1.0s)
  ✓  3 tests/full-flow.spec.ts:13:1 › full flow (1.0s)

  3 passed (3.2s)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "e2e" / "tests"
            tests.mkdir(parents=True)
            for path in expected:
                (root / path).touch()
            result = verifier.enforce_playwright_manifest(
                verifier.CommandResult("passed", 0, output, 3.2),
                root=root,
                expected=expected,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.returncode, 0)

    def test_playwright_evidence_rejects_missing_executed_test(self) -> None:
        expected = {"e2e/tests/account-management.spec.ts": 2}
        output = """\
  ✓  1 tests/account-management.spec.ts:49:1 › first account flow (1.0s)

  1 passed (1.2s)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test_file = root / "e2e" / "tests" / "account-management.spec.ts"
            test_file.parent.mkdir(parents=True)
            test_file.touch()
            result = verifier.enforce_playwright_manifest(
                verifier.CommandResult("passed", 0, output, 1.2),
                root=root,
                expected=expected,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.reason, "Playwright manifest evidence mismatch")
        self.assertIn("expected 2 tests", result.output)
        self.assertIn("executed 1 tests", result.output)

    def test_playwright_evidence_rejects_unregistered_discovered_spec(self) -> None:
        expected = {"e2e/tests/account-management.spec.ts": 1}
        output = """\
  ✓  1 tests/account-management.spec.ts:49:1 › account flow (1.0s)

  1 passed (1.2s)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "e2e" / "tests"
            tests.mkdir(parents=True)
            (tests / "account-management.spec.ts").touch()
            (tests / "unregistered.spec.ts").touch()
            result = verifier.enforce_playwright_manifest(
                verifier.CommandResult("passed", 0, output, 1.2),
                root=root,
                expected=expected,
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("unregistered.spec.ts", result.output)

    def test_manifest_redacts_secrets_if_a_command_argument_contains_one(self) -> None:
        manifest = verifier.build_manifest(
            "2026-07-25T12:00:00+08:00",
            checks={
                "safe": verifier.CheckSpec(
                    ("provider", "API_KEY=must-not-be-persisted")
                )
            },
        )

        command = " ".join(manifest["checks"]["safe"]["command"])
        self.assertNotIn("must-not-be-persisted", command)
        self.assertIn("<redacted>", command)

    def test_manifest_redacts_separate_cli_secret_arguments(self) -> None:
        manifest = verifier.build_manifest(
            "2026-07-25T12:00:00+08:00",
            checks={
                "safe": verifier.CheckSpec(
                    (
                        "provider",
                        "--api-key",
                        "separate-api-secret",
                        "--token=inline-token-secret",
                        "--password",
                        "quoted password secret",
                        "--status-token",
                        "complete",
                    )
                )
            },
        )

        command = " ".join(manifest["checks"]["safe"]["command"])
        for secret in (
            "separate-api-secret",
            "inline-token-secret",
            "quoted password secret",
        ):
            self.assertNotIn(secret, command)
        self.assertIn("--status-token complete", command)


class LogSafetyTests(unittest.TestCase):
    def test_every_example_environment_secret_key_is_redacted(self) -> None:
        example = (verifier.ROOT / ".env.example").read_text(encoding="utf-8")
        secret_keys = []
        for line in example.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0]
            normalized = key.lower()
            if any(
                marker in normalized
                for marker in ("password", "secret", "token", "api_key")
            ):
                secret_keys.append(key)

        self.assertGreater(len(secret_keys), 0)
        for key in secret_keys:
            with self.subTest(key=key):
                safe = verifier.sanitize_log(f"{key}=sentinel-project-secret")
                self.assertNotIn("sentinel-project-secret", safe)

    def test_redaction_covers_credentials_and_bounds_utf8_log_size(self) -> None:
        unsafe = (
            "API_KEY=top-secret\n"
            "password: hunter2\n"
            '"token": "quoted token value"\n'
            "Authorization: Bearer abc.def.ghi\n"
            "postgresql://grader:private@localhost/db\n"
            + ("学生答案" * verifier.MAX_LOG_BYTES)
        )

        safe = verifier.sanitize_log(unsafe)

        self.assertNotIn("top-secret", safe)
        self.assertNotIn("hunter2", safe)
        self.assertNotIn("quoted token value", safe)
        self.assertNotIn("abc.def.ghi", safe)
        self.assertNotIn("private", safe)
        self.assertIn("<redacted>", safe)
        self.assertIn("log truncated", safe)
        self.assertLessEqual(len(safe.encode("utf-8")), verifier.MAX_LOG_BYTES)

    def test_project_secret_keys_are_redacted_across_common_log_formats(self) -> None:
        cases = {
            "dotenv api key": "AGENT_API_KEY=sentinel-agent-api",
            "shell export token": (
                "export MATTERMOST_BOT_TOKEN='sentinel bot token with spaces'"
            ),
            "setup key with colon": 'MM_DEMO_SETUP_KEY: "sentinel setup key"',
            "json password": '"VITE_DEMO_PASSWORD": "sentinel-json-password",',
            "jwt secret": "JWT_SECRET=sentinel-jwt-secret",
            "action secret": "MATTERMOST_ACTION_SECRET: sentinel-action-secret",
            "mattermost command token": (
                "MATTERMOST_COMMAND_TOKEN=sentinel-command-token"
            ),
            "mattermost demo setup": (
                "MATTERMOST_DEMO_SETUP_KEY=sentinel-mattermost-setup"
            ),
            "mattermost database password": (
                "MATTERMOST_DB_PASSWORD=sentinel-mattermost-db"
            ),
            "database password": "POSTGRES_PASSWORD = sentinel-db-password",
            "demo setup key": 'DEMO_SETUP_KEY="sentinel-demo-setup"',
            "hyphenated key": "agent-api-key: sentinel-hyphen-key",
            "bearer authorization": (
                "Authorization: Bearer sentinel.bearer.credentials"
            ),
            "basic authorization": (
                "Proxy-Authorization: Basic c2VudGluZWw6cGFzc3dvcmQ="
            ),
            "json authorization": (
                '"Authorization": "Bearer sentinel-json-authorization"'
            ),
            "equals quoted authorization": (
                'Authorization=Basic "sentinel-quoted-authorization"'
            ),
            "url userinfo": (
                "postgresql://grader:sentinel-url-password@127.0.0.1/grader"
            ),
            "query access token": (
                "https://example.test/callback?access_token=sentinel-query-token&status=ok"
            ),
            "query api key": (
                "https://example.test/callback?api_key=sentinel-query-api#result"
            ),
            "query password": (
                "https://example.test/?password=sentinel-query-password"
            ),
            "cli inline": "provider --api-key=sentinel-cli-inline",
            "cli colon": "provider --bot-token: sentinel-cli-colon",
            "postgres libpq password": "PGPASSWORD=sentinel-libpq-password",
            "aws secret access key": ("AWS_SECRET_ACCESS_KEY=sentinel-aws-secret"),
            "compact api key flag": "provider --apikey=sentinel-compact-api",
        }

        for label, unsafe in cases.items():
            with self.subTest(label=label):
                safe = verifier.sanitize_log(unsafe)
                self.assertNotIn("sentinel", safe)
                self.assertIn("<redacted>", safe)

    def test_non_secret_token_metrics_and_status_text_remain_readable(self) -> None:
        harmless = (
            "token count: 12\n"
            "token_count=12\n"
            "status_token=complete\n"
            "password policy: strong\n"
            "secret name: AGENT_API_KEY\n"
            "request status: passed\n"
            "https://example.test/?token_count=12&status_token=complete\n"
        )

        self.assertEqual(verifier.sanitize_log(harmless), harmless)

    def test_secret_at_truncated_tail_is_redacted_before_size_limiting(self) -> None:
        unsafe = (
            "ordinary output\n" * verifier.MAX_LOG_BYTES
        ) + '"MATTERMOST_COMMAND_TOKEN": "sentinel-tail-token"\n'

        safe = verifier.sanitize_log(unsafe)

        self.assertNotIn("sentinel-tail-token", safe)
        self.assertIn("<redacted>", safe)
        self.assertIn("log truncated", safe)
        self.assertLessEqual(len(safe.encode("utf-8")), verifier.MAX_LOG_BYTES)


class ManagedCommandTests(unittest.TestCase):
    def test_success_and_failure_return_structured_results(self) -> None:
        success = verifier.run_command(
            [sys.executable, "-c", "print('ok')"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=5,
        )
        failure = verifier.run_command(
            [sys.executable, "-c", "print('bad'); raise SystemExit(7)"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=5,
        )

        self.assertEqual((success.status, success.returncode), ("passed", 0))
        self.assertEqual((failure.status, failure.returncode), ("failed", 7))
        self.assertIn("ok", success.output)
        self.assertIn("bad", failure.output)

    def test_missing_executable_is_blocked_not_an_uncaught_exception(self) -> None:
        result = verifier.run_command(
            ["definitely-not-an-ai-grading-command"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=1,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.returncode, 127)
        self.assertIn("unavailable", result.output)

    def test_timeout_terminates_the_owned_process_group(self) -> None:
        started = time.monotonic()
        result = verifier.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=0.1,
        )

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 5)

    def test_keyboard_interrupt_terminates_the_owned_process_group(self) -> None:
        process = MagicMock()
        process.pid = 424242
        process.wait.side_effect = KeyboardInterrupt()

        with (
            patch.object(verifier.subprocess, "Popen", return_value=process),
            patch.object(verifier, "terminate_process_group") as terminate,
            self.assertRaises(KeyboardInterrupt),
        ):
            verifier.run_command(
                ["interrupted"],
                cwd=Path.cwd(),
                env={},
                timeout=1,
            )

        terminate.assert_called_once_with(
            process,
            process_group=process.pid,
            cleanup_deadline=unittest.mock.ANY,
        )

    def test_leader_exit_with_inherited_output_does_not_break_timeout_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            child_code = "import time; time.sleep(5)"
            leader_code = (
                "import subprocess, sys; "
                f"p=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                f"open({str(pid_file)!r}, 'w').write(str(p.pid))"
            )
            started = time.monotonic()
            result = verifier.run_command(
                [sys.executable, "-c", leader_code],
                cwd=Path.cwd(),
                env=os.environ.copy(),
                timeout=0.15,
            )
            elapsed = time.monotonic() - started
            child_pid = int(pid_file.read_text())

        self.assertEqual(result.status, "passed")
        self.assertLess(elapsed, 1.0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_detached_descendant_is_stopped_by_task_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "detached.pid"
            child_code = "import time; time.sleep(5)"
            leader_code = (
                "import subprocess, sys; "
                f"p=subprocess.Popen([sys.executable, '-c', {child_code!r}], "
                "start_new_session=True); "
                f"open({str(pid_file)!r}, 'w').write(str(p.pid))"
            )
            started = time.monotonic()
            result = verifier.run_command(
                [sys.executable, "-c", leader_code],
                cwd=Path.cwd(),
                env=os.environ.copy(),
                timeout=0.15,
            )
            elapsed = time.monotonic() - started
            child_pid = int(pid_file.read_text())

        self.assertEqual(result.status, "passed")
        self.assertLess(elapsed, 1.0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_megabyte_output_is_spooled_and_never_returned_raw(self) -> None:
        result = verifier.run_command(
            [sys.executable, "-c", "print('x' * 1_100_000)"],
            cwd=Path.cwd(),
            env=os.environ.copy(),
            timeout=5,
        )

        self.assertEqual(result.status, "passed")
        self.assertIn("output omitted", result.output)
        self.assertLess(len(result.output.encode()), 1000)

    def test_spool_is_mode_0600_and_has_no_path_during_command_execution(self) -> None:
        created: list[tuple[Path, int]] = []
        real_mkstemp = tempfile.mkstemp

        def tracked_mkstemp(*args, **kwargs):
            descriptor, name = real_mkstemp(*args, **kwargs)
            created.append((Path(name), os.fstat(descriptor).st_mode & 0o777))
            return descriptor, name

        with patch.object(verifier.tempfile, "mkstemp", side_effect=tracked_mkstemp):
            result = verifier.run_command(
                [sys.executable, "-c", "print('safe')"],
                cwd=Path.cwd(),
                env=os.environ.copy(),
                timeout=5,
            )

        self.assertEqual(result.status, "passed")
        self.assertGreaterEqual(len(created), 1)
        for path, mode in created:
            self.assertEqual(mode, 0o600)
            self.assertFalse(path.exists())


class DeadlineAndLockTests(unittest.TestCase):
    def test_deadline_caps_every_phase_and_exposes_one_cleanup_grace(self) -> None:
        clock = MagicMock(side_effect=[100.0, 103.0, 109.5, 111.0])
        deadline = verifier.Deadline(10.0, cleanup_grace=2.0, clock=clock)

        self.assertEqual(deadline.remaining(), 7.0)
        self.assertEqual(deadline.bounded(8.0), 0.5)
        self.assertEqual(deadline.remaining(), 0.0)
        self.assertEqual(deadline.cleanup_deadline, 112.0)

    def test_repository_lock_rejects_a_real_concurrent_process_without_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / ".verify.lock"
            sentinel = root / "second-wrote"
            code = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
                "import verify_acceptance as v; "
                f"lock=Path({str(lock_path)!r}); marker=Path({str(sentinel)!r}); "
                "\ntry:\n"
                "  with v.RepositoryRunLock(lock): marker.write_text('bad')\n"
                "except v.VerificationBusy as exc:\n"
                "  print(str(exc)); raise SystemExit(75)\n"
            )
            with verifier.RepositoryRunLock(lock_path):
                second = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            second_wrote = sentinel.exists()

        self.assertEqual(second.returncode, 75)
        self.assertIn("already running", second.stdout)
        self.assertFalse(second_wrote)

    def test_postgres_factory_receives_the_shared_total_deadline(self) -> None:
        received = []

        class FakePostgres:
            process = None
            port = 65432
            test_database_url = "postgresql+asyncpg://postgres@/grader_test"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

        def factory(deadline):
            received.append(deadline)
            return FakePostgres()

        checks = {
            "backend": verifier.CheckSpec(
                (sys.executable, "-c", "print('ok')"), uses_postgres=True
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = verifier.run_verification(
                checks=checks,
                output=Path(temporary) / "manifest.json",
                postgres_factory=factory,
                total_timeout=2,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], verifier.Deadline)

    def test_cli_explains_total_deadline_and_separate_cleanup_grace(self) -> None:
        help_text = verifier._parser().format_help()

        self.assertIn("setup and checks", help_text)
        self.assertIn("cleanup grace", help_text)


class VerificationFlowTests(unittest.TestCase):
    @staticmethod
    def _one_passing_check():
        return {
            "only": verifier.CheckSpec((sys.executable, "-c", "print('ok')"), timeout=5)
        }

    def test_e2e_gate_rejects_passing_command_without_playwright_manifest(self) -> None:
        checks = {
            "e2e": verifier.CheckSpec(
                (sys.executable, "-c", "print('6 passed (1.0s)')"),
                timeout=5,
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                output=output,
                environment=os.environ.copy(),
                postgres_factory=None,
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["checks"]["e2e"]["status"], "failed")
        self.assertEqual(
            manifest["checks"]["e2e"]["reason"],
            "Playwright manifest evidence mismatch",
        )

    def test_permanent_final_manifest_failure_is_reported_and_never_succeeds(
        self,
    ) -> None:
        checks = self._one_passing_check()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            real_write = verifier.atomic_write_json

            def fail_every_final_write(path, value):
                if value["finished_at"] is not None:
                    raise OSError("sentinel-storage-detail")
                return real_write(path, value)

            with (
                patch.object(
                    verifier,
                    "atomic_write_json",
                    side_effect=fail_every_final_write,
                ),
                patch.object(verifier.sys, "stderr") as stderr,
            ):
                exit_code = verifier.run_verification(
                    checks=checks,
                    output=output,
                    postgres_factory=None,
                    total_timeout=10,
                )
            persisted = json.loads(output.read_text())
            stderr_text = "".join(
                call.args[0] for call in stderr.write.call_args_list if call.args
            )

        self.assertNotEqual(exit_code, 0)
        self.assertNotEqual(persisted["overall_status"], "passed")
        self.assertIn("final evidence persistence failed", stderr_text)
        self.assertNotIn("sentinel-storage-detail", stderr_text)

    def test_one_final_manifest_interrupt_is_sticky_across_successful_retry(
        self,
    ) -> None:
        checks = self._one_passing_check()
        for interrupted_target in ("latest", "run"):
            with self.subTest(interrupted_target=interrupted_target):
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "manifest.json"
                    real_write = verifier.atomic_write_json
                    interrupted_once = False

                    def interrupt_selected_final_write(
                        path,
                        value,
                        expected_output=output,
                        target=interrupted_target,
                        writer=real_write,
                    ):
                        nonlocal interrupted_once
                        is_latest = path == expected_output
                        selected = is_latest if target == "latest" else not is_latest
                        if (
                            selected
                            and not interrupted_once
                            and value["finished_at"] is not None
                        ):
                            interrupted_once = True
                            raise KeyboardInterrupt
                        return writer(path, value)

                    with patch.object(
                        verifier,
                        "atomic_write_json",
                        side_effect=interrupt_selected_final_write,
                    ):
                        exit_code = verifier.run_verification(
                            checks=checks,
                            output=output,
                            postgres_factory=None,
                            total_timeout=10,
                        )
                    latest = json.loads(output.read_text())
                    run_copy = json.loads(
                        (output.parent / latest["evidence_manifest"]).read_text()
                    )

                self.assertTrue(interrupted_once)
                self.assertEqual(exit_code, 130)
                for persisted in (latest, run_copy):
                    self.assertEqual(persisted["overall_status"], "interrupted")
                    self.assertIsNotNone(persisted["finished_at"])
                    self.assertIn(
                        "persistence was interrupted", persisted["internal_error"]
                    )

    def test_partial_final_manifest_failure_marks_the_writable_copy_failed(
        self,
    ) -> None:
        checks = self._one_passing_check()
        for failed_target in ("latest", "run"):
            with self.subTest(failed_target=failed_target):
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "manifest.json"
                    real_write = verifier.atomic_write_json

                    def fail_selected_final_write(
                        path,
                        value,
                        expected_output=output,
                        target=failed_target,
                        writer=real_write,
                    ):
                        is_latest = path == expected_output
                        selected = is_latest if target == "latest" else not is_latest
                        if selected and value["finished_at"] is not None:
                            raise OSError("private-disk-detail")
                        return writer(path, value)

                    with (
                        patch.object(
                            verifier,
                            "atomic_write_json",
                            side_effect=fail_selected_final_write,
                        ),
                        patch.object(verifier.sys, "stderr") as stderr,
                    ):
                        exit_code = verifier.run_verification(
                            checks=checks,
                            output=output,
                            postgres_factory=None,
                            total_timeout=10,
                        )
                    latest = json.loads(output.read_text())
                    run_path = output.parent / latest["evidence_manifest"]
                    run_copy = json.loads(run_path.read_text())

                writable = run_copy if failed_target == "latest" else latest
                stale = latest if failed_target == "latest" else run_copy
                self.assertEqual(exit_code, 1)
                self.assertEqual(writable["overall_status"], "failed")
                self.assertIn("persistence failed", writable["internal_error"])
                self.assertNotEqual(stale["overall_status"], "passed")
                self.assertTrue(stderr.write.called)

    def test_initial_partial_manifest_failure_is_finalized_to_both_targets(
        self,
    ) -> None:
        checks = self._one_passing_check()
        for failed_target in ("latest", "run"):
            with self.subTest(failed_target=failed_target):
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "manifest.json"
                    real_write = verifier.atomic_write_json
                    failed_once = False

                    def fail_one_initial_target(
                        path,
                        value,
                        expected_output=output,
                        target=failed_target,
                        writer=real_write,
                    ):
                        nonlocal failed_once
                        is_latest = path == expected_output
                        selected = is_latest if target == "latest" else not is_latest
                        if (
                            selected
                            and not failed_once
                            and value["finished_at"] is None
                        ):
                            failed_once = True
                            raise OSError("initial write failed")
                        return writer(path, value)

                    with patch.object(
                        verifier,
                        "atomic_write_json",
                        side_effect=fail_one_initial_target,
                    ):
                        exit_code = verifier.run_verification(
                            checks=checks,
                            output=output,
                            postgres_factory=None,
                            total_timeout=10,
                        )
                    latest = json.loads(output.read_text())
                    run_copy = json.loads(
                        (output.parent / latest["evidence_manifest"]).read_text()
                    )

                self.assertEqual(exit_code, 1)
                self.assertEqual(latest["overall_status"], "failed")
                self.assertEqual(run_copy["overall_status"], "failed")
                self.assertIsNotNone(latest["finished_at"])

    def test_sequential_runs_keep_distinct_immutable_evidence_directories(self) -> None:
        checks = {
            "only": verifier.CheckSpec(
                (sys.executable, "-c", "print('retained evidence')"), timeout=5
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            first_exit = verifier.run_verification(
                checks=checks,
                output=output,
                postgres_factory=None,
                total_timeout=10,
            )
            first = json.loads(output.read_text())
            first_manifest = output.parent / first["evidence_manifest"]
            first_log = output.parent / first["checks"]["only"]["evidence_log"]
            second_exit = verifier.run_verification(
                checks=checks,
                output=output,
                postgres_factory=None,
                total_timeout=10,
            )
            second = json.loads(output.read_text())

            self.assertTrue(first_manifest.is_file())
            self.assertTrue(first_log.is_file())
            self.assertIn("retained evidence", first_log.read_text())

        self.assertEqual((first_exit, second_exit), (0, 0))
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_evidence_root_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside"
            target.mkdir()
            evidence = root / "evidence"
            evidence.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "real directory"):
                verifier._create_run_directory(evidence / "manifest.json", "run")

            self.assertEqual(list(target.iterdir()), [])

    def test_failure_does_not_prevent_independent_checks_and_sets_nonzero_exit(
        self,
    ) -> None:
        checks = {
            "first": verifier.CheckSpec(
                (sys.executable, "-c", "raise SystemExit(9)"), timeout=5
            ),
            "second": verifier.CheckSpec(
                (sys.executable, "-c", "print('still ran')"), timeout=5
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                selected=set(checks),
                output=output,
                environment=os.environ.copy(),
                postgres_factory=None,
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["checks"]["first"]["status"], "failed")
        self.assertEqual(manifest["checks"]["second"]["status"], "passed")
        self.assertIsNotNone(manifest["finished_at"])

    def test_dependency_failure_blocks_only_its_dependents(self) -> None:
        checks = {
            "dependency": verifier.CheckSpec(
                (sys.executable, "-c", "raise SystemExit(2)"), timeout=5
            ),
            "dependent": verifier.CheckSpec(
                (sys.executable, "-c", "raise SystemExit(99)"),
                timeout=5,
                requires=("dependency",),
            ),
            "independent": verifier.CheckSpec(
                (sys.executable, "-c", "print('ok')"), timeout=5
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                selected=set(checks),
                output=output,
                environment=os.environ.copy(),
                postgres_factory=None,
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["checks"]["dependent"]["status"], "blocked")
        self.assertEqual(manifest["checks"]["dependent"]["returncode"], None)
        self.assertEqual(manifest["checks"]["independent"]["status"], "passed")

    def test_unselected_checks_are_skipped_and_do_not_fail_local_run(self) -> None:
        checks = {
            "selected": verifier.CheckSpec(
                (sys.executable, "-c", "print('ok')"), timeout=5
            ),
            "docker_only": verifier.CheckSpec(("missing-docker",), timeout=5),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                selected={"selected"},
                output=output,
                environment=os.environ.copy(),
                postgres_factory=None,
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["checks"]["selected"]["status"], "passed")
        self.assertEqual(manifest["checks"]["docker_only"]["status"], "skipped")

    def test_missing_locked_dependencies_produce_a_clear_blocked_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "node_modules" / ".package-lock.json"
            checks = {
                "frontend": verifier.CheckSpec(
                    (sys.executable, "-c", "raise SystemExit(99)"),
                    timeout=5,
                    required_paths=(missing,),
                    install_hint="make install-frontend",
                )
            }
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                selected={"frontend"},
                output=output,
                environment=os.environ.copy(),
                postgres_factory=None,
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 1)
        entry = manifest["checks"]["frontend"]
        self.assertEqual(entry["status"], "blocked")
        self.assertIn("make install-frontend", entry["reason"])

    def test_interrupt_writes_final_manifest_and_returns_shell_interrupt_code(
        self,
    ) -> None:
        checks = {"only": verifier.CheckSpec(("interrupt-me",), timeout=5)}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            with patch.object(verifier, "run_command", side_effect=KeyboardInterrupt):
                exit_code = verifier.run_verification(
                    checks=checks,
                    selected={"only"},
                    output=output,
                    environment={},
                    postgres_factory=None,
                    total_timeout=20,
                )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 130)
        self.assertEqual(manifest["checks"]["only"]["status"], "interrupted")
        self.assertIsNotNone(manifest["finished_at"])

    def test_interrupt_during_initial_manifest_write_still_finalizes_evidence(
        self,
    ) -> None:
        checks = {"only": verifier.CheckSpec((sys.executable, "-c", "print('no')"))}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            real_write = verifier.atomic_write_json
            calls = 0

            def interrupt_once(path, value):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt
                return real_write(path, value)

            with patch.object(
                verifier, "atomic_write_json", side_effect=interrupt_once
            ):
                exit_code = verifier.run_verification(
                    checks=checks,
                    output=output,
                    postgres_factory=None,
                    total_timeout=2,
                )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 130)
        self.assertEqual(manifest["overall_status"], "interrupted")
        self.assertIsNotNone(manifest["finished_at"])
        self.assertIn(manifest["checks"]["only"]["status"], {"interrupted", "skipped"})

    def test_keyboard_interrupt_during_cleanup_is_recorded_not_raised(self) -> None:
        class InterruptingCleanupPostgres:
            process = None
            port = 65432
            test_database_url = "postgresql+asyncpg://postgres@/grader_test"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                raise KeyboardInterrupt

        checks = {
            "backend": verifier.CheckSpec(
                (sys.executable, "-c", "print('ok')"), uses_postgres=True
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                output=output,
                postgres_factory=lambda _deadline: InterruptingCleanupPostgres(),
                total_timeout=2,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 130)
        self.assertEqual(manifest["overall_status"], "interrupted")
        self.assertIsNotNone(manifest["finished_at"])
        self.assertIn("cleanup_error", manifest)

    def test_second_termination_signal_is_ignored_while_finalizing(self) -> None:
        with verifier._termination_as_interrupt():
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)

    def test_postgres_cleanup_failure_makes_the_overall_result_fail(self) -> None:
        class BrokenCleanupPostgres:
            test_database_url = (
                "postgresql+asyncpg://postgres@127.0.0.1:65432/grader_test"
            )
            process = None
            port = 65432

            def __enter__(self):
                return self

            def __exit__(self, *_exc: object) -> None:
                raise RuntimeError("cleanup failed")

        checks = {
            "backend": verifier.CheckSpec(
                (sys.executable, "-c", "print('ok')"),
                timeout=5,
                uses_postgres=True,
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            exit_code = verifier.run_verification(
                checks=checks,
                selected={"backend"},
                output=output,
                environment=os.environ.copy(),
                postgres_factory=lambda _deadline: BrokenCleanupPostgres(),
                total_timeout=20,
            )
            manifest = json.loads(output.read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["overall_status"], "failed")
        self.assertIn("cleanup_error", manifest)


class TemporaryPostgresCleanupTests(unittest.TestCase):
    def _postgres(self, temporary: Path):
        postgres = object.__new__(verifier.TemporaryPostgres)
        postgres.deadline = verifier.Deadline(20, cleanup_grace=0.05)
        postgres.process = MagicMock()
        postgres.process.pid = 4242
        postgres.log_handle = MagicMock()
        postgres.temporary = temporary
        return postgres

    def test_cleanup_stops_exact_owned_group_before_removing_pgdata(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-test-"))
        postgres = self._postgres(temporary)
        process = postgres.process
        log_handle = postgres.log_handle

        with patch.object(verifier, "terminate_process_group") as terminate:
            postgres.close()

        terminate.assert_called_once_with(
            process,
            process_group=4242,
            cleanup_deadline=unittest.mock.ANY,
        )
        log_handle.close.assert_called_once()
        self.assertFalse(temporary.exists())

    def test_cleanup_retains_pgdata_if_owned_process_cannot_be_stopped(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-test-"))
        postgres = self._postgres(temporary)
        log_handle = postgres.log_handle

        try:
            with (
                patch.object(
                    verifier,
                    "terminate_process_group",
                    side_effect=RuntimeError("still running"),
                ),
                patch.object(verifier, "_group_exists", return_value=True),
                self.assertRaisesRegex(RuntimeError, "still running"),
            ):
                postgres.close()

            log_handle.close.assert_called_once()
            self.assertTrue(temporary.exists())
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def test_cleanup_rechecks_exact_group_after_a_stop_race(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-test-"))
        postgres = self._postgres(temporary)
        postgres.process.poll.return_value = 0

        with (
            patch.object(
                verifier,
                "terminate_process_group",
                side_effect=RuntimeError("group disappeared at deadline"),
            ),
            patch.object(verifier, "_group_exists", return_value=False),
        ):
            postgres.close()

        self.assertFalse(temporary.exists())

    def test_cleanup_retains_and_reports_pgdata_if_removal_fails(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-test-"))
        postgres = self._postgres(temporary)
        try:
            with (
                patch.object(verifier, "terminate_process_group"),
                patch.object(verifier.shutil, "rmtree", side_effect=OSError("denied")),
                patch.object(verifier.sys, "stderr") as stderr,
                self.assertRaisesRegex(RuntimeError, "retained"),
            ):
                postgres.close()

            self.assertTrue(temporary.exists())
            self.assertTrue(stderr.write.called)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def test_cleanup_retries_transient_directory_mutation(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="ai-grading-verify-test-"))
        postgres = self._postgres(temporary)
        real_rmtree = shutil.rmtree
        calls = 0

        def transient_rmtree(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("directory changed during removal")
            return real_rmtree(path)

        with (
            patch.object(verifier, "terminate_process_group"),
            patch.object(verifier.shutil, "rmtree", side_effect=transient_rmtree),
        ):
            postgres.close()

        self.assertEqual(calls, 2)
        self.assertFalse(temporary.exists())


class TemporaryPostgresOwnershipTests(unittest.TestCase):
    def _postgres(self, temporary: Path):
        postgres = object.__new__(verifier.TemporaryPostgres)
        postgres.deadline = verifier.Deadline(20)
        postgres.pgdata = temporary / "postgres"
        postgres.socket_dir = temporary / "socket"
        postgres.port = 54329
        postgres.nonce = "owned-nonce"
        postgres.postgres = "postgres"
        postgres.psql = "psql"
        postgres.createdb = "createdb"
        return postgres

    def test_database_url_uses_the_private_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            postgres = self._postgres(Path(temporary))

            url = postgres.test_database_url

        self.assertNotIn("127.0.0.1", url)
        self.assertIn("host=", url)
        self.assertIn("socket", url)

    def test_wrong_server_identity_refuses_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            postgres = self._postgres(Path(temporary))
            calls = []

            def checked(command, timeout, label):
                calls.append(tuple(command))
                if command[0] == "psql":
                    return "/some/other/data\nwrong-nonce\n"
                return ""

            postgres._checked = checked
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                postgres._verify_ownership_and_create_database()

        self.assertFalse(any(command[0] == "createdb" for command in calls))

    def test_postgres_command_binds_only_the_private_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            postgres = self._postgres(Path(temporary))

            command = postgres._server_command()

        self.assertIn("-h", command)
        self.assertEqual(command[command.index("-h") + 1], "")
        self.assertEqual(command[command.index("-k") + 1], str(postgres.socket_dir))
        self.assertIn("ai_grading.verification_nonce=owned-nonce", command)


class MakefileContractTests(unittest.TestCase):
    def test_make_verify_uses_unified_runner_without_demo_recursion(self) -> None:
        makefile = (verifier.ROOT / "Makefile").read_text(encoding="utf-8")
        verify_recipe = makefile.split("\nverify:", 1)[1].split("\n\n", 1)[0]
        demo_recipe = makefile.split("\ndemo:", 1)[1].split("\n\n", 1)[0]

        self.assertIn("scripts/verify_acceptance.py", verify_recipe)
        self.assertIn("--local-only", makefile)
        self.assertIn("command -v python3", makefile)
        self.assertNotIn("verify_acceptance.py", demo_recipe)


if __name__ == "__main__":
    unittest.main()
