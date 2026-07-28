from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_local_e2e as runner


class LocalRunnerTests(unittest.TestCase):
    def test_dependency_resolution_failure_does_not_create_a_temporary_directory(
        self,
    ) -> None:
        with (
            patch.object(
                runner,
                "executable",
                side_effect=runner.StackError("missing dependency"),
            ),
            patch.object(runner.tempfile, "mkdtemp") as make_temporary,
            self.assertRaisesRegex(runner.StackError, "missing dependency"),
        ):
            runner.LocalStack()

        make_temporary.assert_not_called()

    def test_managed_command_times_out_and_terminates_its_process_group(self) -> None:
        process = MagicMock()
        process.pid = 12345
        process.wait.side_effect = runner.subprocess.TimeoutExpired(["slow"], 0.01)

        with (
            patch.object(runner.subprocess, "Popen", return_value=process) as popen,
            patch.object(runner, "terminate_group") as terminate,
            self.assertRaisesRegex(runner.StackError, "timed out"),
        ):
            runner.run_checked(["slow"], timeout=0.01, name="slow phase")

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        terminate.assert_called_once_with(process, "slow phase")

    def test_managed_command_terminates_its_process_group_when_runner_is_interrupted(
        self,
    ) -> None:
        process = MagicMock()
        process.pid = 12346
        process.wait.side_effect = KeyboardInterrupt()

        with (
            patch.object(runner.subprocess, "Popen", return_value=process),
            patch.object(runner, "terminate_group") as terminate,
            self.assertRaises(KeyboardInterrupt),
        ):
            runner.run_checked(["interrupted"], name="interrupted phase")

        terminate.assert_called_once_with(process, "interrupted phase")

    def test_unique_port_reservation_retries_duplicates_and_closes_every_socket(
        self,
    ) -> None:
        sockets = [MagicMock(), MagicMock()]
        sockets[0].getsockname.return_value = ("127.0.0.1", 51000)
        sockets[1].getsockname.return_value = ("127.0.0.1", 51001)
        context_managers = []
        for socket_mock in sockets:
            context = MagicMock()
            context.__enter__.return_value = socket_mock
            context_managers.append(context)

        with patch.object(runner.socket, "socket", side_effect=context_managers):
            port = runner.reserve_port({51000}, attempts=2)

        self.assertEqual(port, 51001)
        for socket_mock in sockets:
            socket_mock.bind.assert_called_once_with(("127.0.0.1", 0))
        for context in context_managers:
            context.__exit__.assert_called_once()

    def test_postgres_pid_requires_owned_pgdata_port_and_process_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runner-identity-") as temporary:
            temporary_path = Path(temporary)
            pgdata = temporary_path / "postgres"
            pgdata.mkdir()
            pid_file = pgdata / "postmaster.pid"
            pid_file.write_text(
                "4242\n" + str(pgdata) + "\n0\n55432\n",
                encoding="utf-8",
            )
            command = f"/opt/homebrew/bin/postgres -D {pgdata} -p 55432"

            with patch.object(runner, "read_process_command", return_value=command):
                self.assertEqual(
                    runner.owned_postgres_pid(
                        pgdata, temporary_path, expected_port=55432
                    ),
                    4242,
                )
            with patch.object(
                runner,
                "read_process_command",
                return_value=f"python -D {pgdata} -p 55432",
            ):
                self.assertIsNone(
                    runner.owned_postgres_pid(
                        pgdata, temporary_path, expected_port=55432
                    )
                )
            with patch.object(runner, "read_process_command", return_value=command):
                self.assertIsNone(
                    runner.owned_postgres_pid(
                        pgdata, temporary_path, expected_port=55433
                    )
                )
            pid_file.write_text(
                "4242\n/tmp/not-this-task\n0\n55432\n",
                encoding="utf-8",
            )
            with patch.object(runner, "read_process_command", return_value=command):
                self.assertIsNone(
                    runner.owned_postgres_pid(
                        pgdata, temporary_path, expected_port=55432
                    )
                )

    def test_cleanup_attempts_every_resource_after_failure_and_half_started_postgres(
        self,
    ) -> None:
        temp_path = Path(tempfile.mkdtemp(prefix="ai-grading-e2e-"))
        stack = self._cleanup_stack(temp_path, postgres_attempted=True)
        stack.test_process = MagicMock(name="test")
        stack.web_process = MagicMock(name="web")
        stack.api_process = MagicMock(name="api")
        stack.web_log_handle = MagicMock(name="web-log")
        stack.api_log_handle = MagicMock(name="api-log")

        try:
            with (
                patch.object(
                    runner,
                    "terminate_group",
                    side_effect=[RuntimeError("test cleanup failed"), None, None],
                ) as terminate,
                patch("builtins.print"),
            ):
                errors = stack.cleanup()

            self.assertEqual(terminate.call_count, 3)
            stack.web_log_handle.close.assert_called_once()
            stack.api_log_handle.close.assert_called_once()
            stack._stop_postgres.assert_called_once()
            self.assertFalse(temp_path.exists())
            self.assertTrue(stack.cleaned)
            self.assertFalse(stack.cleaning)
            self.assertEqual([label for label, _ in errors], ["Playwright"])
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_postgres_cleanup_failure_retains_owned_temporary_directory(self) -> None:
        temp_path = Path(tempfile.mkdtemp(prefix="ai-grading-e2e-"))
        stack = self._cleanup_stack(temp_path, postgres_attempted=True)
        stack._stop_postgres.side_effect = runner.StackError("still running")
        try:
            with patch("builtins.print") as output:
                errors = stack.cleanup()
            self.assertTrue(temp_path.is_dir())
            self.assertEqual([label for label, _ in errors], ["PostgreSQL"])
            self.assertTrue(
                any(str(temp_path) in str(call) for call in output.call_args_list)
            )
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def test_normal_and_never_started_cleanup_remove_owned_temporary_directory(
        self,
    ) -> None:
        for attempted in (False, True):
            with self.subTest(postgres_attempted=attempted):
                temp_path = Path(tempfile.mkdtemp(prefix="ai-grading-e2e-"))
                stack = self._cleanup_stack(temp_path, postgres_attempted=attempted)
                with patch("builtins.print"):
                    errors = stack.cleanup()
                self.assertEqual(errors, ())
                self.assertFalse(temp_path.exists())
                if attempted:
                    stack._stop_postgres.assert_called_once()
                else:
                    stack._stop_postgres.assert_not_called()

    def test_http_pair_retries_with_fresh_distinct_ports_after_verified_conflict(
        self,
    ) -> None:
        stack = object.__new__(runner.LocalStack)
        stack.pg_port = 50000
        stack.api_port = None
        stack.web_port = None
        stack._start_http_pair_attempt = MagicMock(
            side_effect=[runner.PortConflictError("port grabbed"), None]
        )
        stack._reset_http_attempt = MagicMock()

        with patch.object(
            runner,
            "reserve_port",
            side_effect=[51000, 51001, 52000, 52001],
        ):
            stack._start_http_services()

        self.assertEqual(stack.api_port, 52000)
        self.assertEqual(stack.web_port, 52001)
        self.assertEqual(stack._start_http_pair_attempt.call_count, 2)
        stack._reset_http_attempt.assert_called_once()

    def test_postgres_retries_a_proven_bind_conflict_with_a_fresh_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runner-pg-port-") as temporary:
            stack = object.__new__(runner.LocalStack)
            stack.pgdata = Path(temporary) / "postgres"
            stack.pgdata.mkdir()
            stack.pglog = Path(temporary) / "postgres.log"
            stack.pg_ctl = "pg_ctl"
            stack.pg_start_attempted = False
            calls = 0

            def start(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    stack.pglog.write_text(
                        "could not bind IPv4 address: Address already in use\n",
                        encoding="utf-8",
                    )
                    raise runner.subprocess.CalledProcessError(1, ["pg_ctl"])

            with (
                patch.object(runner, "reserve_port", side_effect=[51000, 52000]),
                patch.object(runner, "run_checked", side_effect=start),
            ):
                stack._start_postgres()

            self.assertEqual(calls, 2)
            self.assertEqual(stack.pg_port, 52000)
            self.assertTrue(stack.database_url.endswith(":52000/grader_e2e"))
            self.assertTrue(stack.pg_start_attempted)

    def test_http_start_detects_a_port_grabbed_by_another_listener(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="runner-port-grab-") as temporary,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupying_listener,
        ):
            occupying_listener.bind(("127.0.0.1", 0))
            occupying_listener.listen()
            occupied_port = int(occupying_listener.getsockname()[1])
            stack = object.__new__(runner.LocalStack)
            stack.api_port = occupied_port
            stack.web_port = occupied_port + 1
            stack.api_log = Path(temporary) / "api.log"
            stack.web_log = Path(temporary) / "web.log"
            stack.api_log_handle = None
            stack.web_log_handle = None
            stack.api_process = None
            stack.web_process = None
            stack.lsof = "/usr/sbin/lsof"
            stack.environment = MagicMock(return_value=dict(runner.os.environ))
            stack._api_command = MagicMock(
                return_value=[
                    sys.executable,
                    "-c",
                    (
                        "import socket; s=socket.socket(); "
                        f"s.bind(('127.0.0.1', {occupied_port}))"
                    ),
                ]
            )

            with (
                self.assertRaises(runner.PortConflictError),
                patch("builtins.print"),
            ):
                stack._start_http_pair_attempt()
            with patch("builtins.print"):
                stack._reset_http_attempt()

    def test_http_pair_does_not_retry_a_non_bind_startup_failure(self) -> None:
        stack = object.__new__(runner.LocalStack)
        stack.pg_port = 50000
        stack.api_port = None
        stack.web_port = None
        stack._start_http_pair_attempt = MagicMock(
            side_effect=runner.StackError("application import failed")
        )
        stack._reset_http_attempt = MagicMock()

        with (
            patch.object(runner, "reserve_port", side_effect=[51000, 51001]),
            self.assertRaisesRegex(runner.StackError, "application import failed"),
        ):
            stack._start_http_services()

        stack._start_http_pair_attempt.assert_called_once()
        stack._reset_http_attempt.assert_not_called()

    def test_only_explicit_bind_messages_are_classified_as_port_conflicts(self) -> None:
        self.assertTrue(
            runner.is_explicit_port_conflict("[Errno 48] Address already in use")
        )
        self.assertTrue(
            runner.is_explicit_port_conflict(
                "Error: Port 5173 is already in use",
                port=5173,
            )
        )
        self.assertFalse(
            runner.is_explicit_port_conflict(
                "Error: Port 5173 is already in use",
                port=5174,
            )
        )
        self.assertFalse(runner.is_explicit_port_conflict("database migration failed"))
        self.assertFalse(runner.is_explicit_port_conflict("readiness timed out"))

    @staticmethod
    def _cleanup_stack(temp_path: Path, *, postgres_attempted: bool):
        stack = object.__new__(runner.LocalStack)
        stack.cleaned = False
        stack.cleaning = False
        stack.cleanup_errors = []
        stack.test_process = None
        stack.web_process = None
        stack.api_process = None
        stack.web_log_handle = None
        stack.api_log_handle = None
        stack.pg_start_attempted = postgres_attempted
        stack.pgdata = temp_path / "postgres"
        stack.temp_path = temp_path
        stack._stop_postgres = MagicMock(name="stop-postgres")
        return stack


if __name__ == "__main__":
    unittest.main()
