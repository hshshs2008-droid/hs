import io
import json
import logging
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import run_bot


class FakeStop:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.delays = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            self.stopped = True
        return self.stopped


def exited_child(code=0):
    child = Mock()
    child.pid = 987654321
    child.poll.return_value = code
    child.wait.return_value = code
    child.stdout = io.StringIO("")
    return child


class SupervisorTests(unittest.TestCase):
    def supervisor(self, stop, popen, clock=lambda: 0):
        return run_bot.Supervisor(Mock(), Mock(), popen=popen, clock=clock,
                                  stop_event=stop, environ={})

    def test_exit_zero_restarts_with_bounded_exponential_backoff(self):
        stop = FakeStop(6)
        popen = Mock(side_effect=lambda *args, **kwargs: exited_child(0))
        supervisor = self.supervisor(stop, popen)
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(stop.delays, [5, 10, 20, 40, 60, 60])
        self.assertEqual(popen.call_count, 6)
        command = popen.call_args.args[0]
        self.assertEqual(command, [run_bot.sys.executable, "-u", str(run_bot.BOT_SCRIPT)])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["pass_fds"],
                         (supervisor.lock.file.fileno(),))

    def test_nonzero_exit_and_launch_failure_both_restart(self):
        stop = FakeStop(3)
        popen = Mock(side_effect=[OSError("secret must not reach logs"),
                                 exited_child(7), exited_child(-9)])
        supervisor = self.supervisor(stop, popen)
        supervisor.run()
        self.assertEqual(stop.delays, [5, 10, 20])
        logged = str(supervisor.logger.mock_calls)
        self.assertNotIn("secret must not reach logs", logged)
        self.assertIn("OSError", logged)

    def test_backoff_resets_only_after_stable_runtime(self):
        stop = FakeStop(4)
        popen = Mock(side_effect=lambda *args, **kwargs: exited_child())
        times = iter([0, 0, 1, 2, 10, 310, 311, 312])
        supervisor = self.supervisor(stop, popen, clock=lambda: next(times))
        supervisor.run()
        self.assertEqual(stop.delays, [5, 10, 5, 10])

    def test_stop_during_backoff_never_starts_another_child(self):
        stop = FakeStop(1)
        popen = Mock(return_value=exited_child())
        self.supervisor(stop, popen).run()
        popen.assert_called_once()

    def test_intentional_stop_forwards_signal_and_does_not_restart(self):
        stop = FakeStop(1)
        child = exited_child()
        child.poll.return_value = None
        popen = Mock(return_value=child)
        supervisor = self.supervisor(stop, popen)
        supervisor.stop_signal = signal.SIGINT
        supervisor.run()
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.wait.assert_called_once_with(timeout=run_bot.STOP_TIMEOUT)
        child.kill.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(stop.delays, [0.25])

    def test_stubborn_child_is_killed_only_after_graceful_timeout(self):
        child = exited_child()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", run_bot.STOP_TIMEOUT), -9]
        supervisor = self.supervisor(FakeStop(1), Mock(return_value=child))
        supervisor.run()
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list[-1].kwargs, {"timeout": run_bot.KILL_TIMEOUT})

    def test_preexisting_stop_never_starts_child(self):
        stop = FakeStop(1)
        stop.set()
        popen = Mock()
        self.supervisor(stop, popen).run()
        popen.assert_not_called()

    def test_configured_database_path_is_preserved_and_normalized(self):
        supervisor = run_bot.Supervisor(Mock(), Mock(), environ={"ALERT_STATE_PATH": "custom/state.sqlite3"})
        self.assertEqual(supervisor.environ["ALERT_STATE_PATH"],
                         str(Path("custom/state.sqlite3").resolve()))
        self.assertEqual(run_bot.runtime_directory({}), run_bot.PROJECT / ".state")
        self.assertEqual(run_bot.runtime_directory({"ALERT_STATE_PATH": "custom/state.sqlite3"}),
                         Path("custom").resolve())

    def test_real_fixture_exit_zero_is_captured_and_restarted(self):
        class FixtureStop(FakeStop):
            def wait(self, delay):
                if delay < 1:
                    time.sleep(0.01)
                    return self.stopped
                return super().wait(delay)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.py"
            fixture.write_text("print('fixture completed', flush=True)\n")
            lock = run_bot.SupervisorLock(directory).acquire()
            logger = run_bot.make_logger(directory)
            try:
                stop = FixtureStop(2)
                with patch("run_bot.BOT_SCRIPT", fixture):
                    supervisor = run_bot.Supervisor(lock, logger, stop_event=stop, environ={})
                    self.assertEqual(supervisor.run(), 0)
                output = Path(directory, "bot.log").read_text()
                self.assertEqual(output.count("fixture completed"), 2)
                self.assertEqual(output.count("exit_code=0"), 2)
                self.assertEqual(stop.delays, [5, 10])
            finally:
                lock.close()
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)


class LockTests(unittest.TestCase):
    def orphan_metadata(self):
        return {"supervisor_pid": 1234567, "supervisor_start_ticks": 100,
                "script_path": str(run_bot.SCRIPT),
                "child_pid": 2345678, "child_start_ticks": 200}

    def test_legacy_live_orphan_blocks_launch_without_overwriting_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = self.orphan_metadata()
            path = Path(directory) / "bot.lock"
            path.write_text(json.dumps(metadata))
            before = path.read_text()
            with patch("run_bot._start_ticks", side_effect=lambda pid: 200 if pid == 2345678 else None), \
                    patch("run_bot._script_matches", side_effect=lambda pid, script:
                          pid == 2345678 and script == run_bot.BOT_SCRIPT), \
                    patch("run_bot.runtime_directory", return_value=Path(directory)), \
                    patch("run_bot.Supervisor") as supervisor, \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(run_bot.main([]), 2)
                supervisor.assert_not_called()
                status = run_bot.supervisor_status(directory)
                self.assertTrue(status["orphan_child_alive"])
                self.assertTrue(status["child_alive"])
                self.assertFalse(status["supervisor_alive"])
            self.assertEqual(path.read_text(), before)

    def test_reused_or_unrelated_child_pid_does_not_block_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            for ticks, matches in ((201, True), (200, False)):
                with self.subTest(ticks=ticks, matches=matches):
                    path.write_text(json.dumps(self.orphan_metadata()))
                    with patch("run_bot._start_ticks", return_value=ticks), \
                            patch("run_bot._script_matches", return_value=matches):
                        lock = run_bot.SupervisorLock(directory).acquire()
                        try:
                            self.assertEqual(lock.metadata["supervisor_pid"], run_bot.os.getpid())
                        finally:
                            lock.close()

    def test_inherited_descriptor_keeps_lock_after_supervisor_descriptor_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = run_bot.SupervisorLock(directory).acquire()
            lock.update(**self.orphan_metadata())
            inherited = run_bot.os.dup(lock.file.fileno())
            lock.file.close()
            lock.file = None
            try:
                with self.assertRaises(run_bot.AlreadyRunning):
                    run_bot.SupervisorLock(directory).acquire()
                with patch("run_bot._start_ticks", side_effect=lambda pid: 200 if pid == 2345678 else None), \
                        patch("run_bot._script_matches", side_effect=lambda pid, script:
                              pid == 2345678 and script == run_bot.BOT_SCRIPT):
                    status = run_bot.supervisor_status(directory)
                    self.assertTrue(status["lock_held"])
                    self.assertTrue(status["orphan_child_alive"])
            finally:
                run_bot.os.close(inherited)

    def test_lock_held_before_child_metadata_is_known_refuses_unsafe_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = run_bot.SupervisorLock(directory).acquire()
            inherited = run_bot.os.dup(lock.file.fileno())
            lock.file.close()
            lock.file = None
            try:
                with patch("run_bot._script_matches", return_value=False), \
                        patch("run_bot.os.kill") as kill, patch("run_bot.os.pidfd_open") as pidfd:
                    status = run_bot.supervisor_status(directory)
                    self.assertTrue(status["unidentified_lock_holder"])
                    self.assertFalse(status["orphan_child_alive"])
                    self.assertFalse(run_bot.stop_supervisor(directory))
                    kill.assert_not_called()
                    pidfd.assert_not_called()
                with self.assertRaises(run_bot.AlreadyRunning):
                    run_bot.SupervisorLock(directory).acquire()
            finally:
                run_bot.os.close(inherited)

    def test_absolute_script_verification_does_not_require_process_cwd(self):
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if str(path).endswith("/cwd"):
                raise PermissionError("process cwd unavailable")
            return original_resolve(path, *args, **kwargs)

        absolute_command = b"python3\0-B\0-u\0" + bytes(run_bot.SCRIPT) + b"\0"
        with patch.object(Path, "read_bytes", return_value=absolute_command), \
                patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertTrue(run_bot._script_matches(123, run_bot.SCRIPT))
        with patch.object(Path, "read_bytes", return_value=b"python3\0run_bot.py\0"), \
                patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertFalse(run_bot._script_matches(123, run_bot.SCRIPT))

    def test_duplicate_lock_is_rejected_without_overwriting_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            first = run_bot.SupervisorLock(directory).acquire()
            try:
                before = Path(directory, "bot.lock").read_text()
                with self.assertRaises(run_bot.AlreadyRunning):
                    run_bot.SupervisorLock(directory).acquire()
                self.assertEqual(Path(directory, "bot.lock").read_text(), before)
            finally:
                first.close()
            second = run_bot.SupervisorLock(directory).acquire()
            second.close()

    def test_status_of_missing_directory_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent"
            self.assertFalse(run_bot.supervisor_status(absent)["lock_held"])
            self.assertFalse(absent.exists())

    def test_live_metadata_requires_matching_process_start_and_script(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = run_bot.SupervisorLock(directory).acquire()
            try:
                with patch("run_bot._script_matches", return_value=True):
                    self.assertTrue(run_bot.supervisor_status(directory)["supervisor_alive"])
                with patch("run_bot._start_ticks", return_value=-1):
                    self.assertFalse(run_bot.supervisor_status(directory)["supervisor_alive"])
                with patch("run_bot._script_matches", return_value=False):
                    self.assertFalse(run_bot.supervisor_status(directory)["supervisor_alive"])
            finally:
                lock.close()
            self.assertFalse(run_bot.supervisor_status(directory)["supervisor_alive"])

    def test_invalid_metadata_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            for contents in ("not json", "[]", json.dumps({"supervisor_pid": 1})):
                path.write_text(contents)
                self.assertFalse(run_bot.supervisor_status(directory)["supervisor_alive"])

    def test_stop_refuses_unverified_owner(self):
        with patch("run_bot.supervisor_status", return_value={"supervisor_alive": False}), \
                patch("run_bot.os.kill") as kill, patch("run_bot.os.pidfd_open") as pidfd:
            self.assertFalse(run_bot.stop_supervisor("unused"))
            kill.assert_not_called()
            pidfd.assert_not_called()

    def test_stop_uses_pinned_verified_owner(self):
        status = {"supervisor_alive": True, "supervisor_pid": 123, "supervisor_start_ticks": 456}
        with patch("run_bot.supervisor_status", return_value=status), \
                patch("run_bot.os.pidfd_open", return_value=99) as pidfd, \
                patch("run_bot._start_ticks", return_value=456), \
                patch("run_bot._script_matches", return_value=True), \
                patch("run_bot.signal.pidfd_send_signal") as send, patch("run_bot.os.close") as close:
            self.assertTrue(run_bot.stop_supervisor("unused"))
            pidfd.assert_called_once_with(123)
            send.assert_called_once_with(99, signal.SIGTERM)
            close.assert_called_once_with(99)

    def test_stop_uses_sigint_only_for_a_pinned_verified_orphan(self):
        status = {"supervisor_alive": False, "orphan_child_alive": True,
                  "child_pid": 123, "child_start_ticks": 456}
        with patch("run_bot.supervisor_status", return_value=status), \
                patch("run_bot.os.pidfd_open", return_value=99), \
                patch("run_bot._start_ticks", return_value=456), \
                patch("run_bot._script_matches", return_value=True) as script_matches, \
                patch("run_bot.signal.pidfd_send_signal") as send, patch("run_bot.os.close"):
            self.assertTrue(run_bot.stop_supervisor("unused"))
            script_matches.assert_called_once_with(123, run_bot.BOT_SCRIPT)
            send.assert_called_once_with(99, signal.SIGINT)

    def test_reused_orphan_pid_is_never_signalled(self):
        status = {"supervisor_alive": False, "orphan_child_alive": True,
                  "child_pid": 123, "child_start_ticks": 456}
        with patch("run_bot.supervisor_status", return_value=status), \
                patch("run_bot.os.pidfd_open", return_value=99), \
                patch("run_bot._start_ticks", return_value=789), \
                patch("run_bot.signal.pidfd_send_signal") as send, patch("run_bot.os.close"):
            self.assertFalse(run_bot.stop_supervisor("unused"))
            send.assert_not_called()

    def test_pid_reuse_between_status_and_pin_never_sends_signal(self):
        status = {"supervisor_alive": True, "supervisor_pid": 123, "supervisor_start_ticks": 456}
        with patch("run_bot.supervisor_status", return_value=status), \
                patch("run_bot.os.pidfd_open", return_value=99), \
                patch("run_bot._start_ticks", return_value=789), \
                patch("run_bot.signal.pidfd_send_signal") as send, patch("run_bot.os.close"):
            self.assertFalse(run_bot.stop_supervisor("unused"))
            send.assert_not_called()


class LoggingTests(unittest.TestCase):
    def test_logs_capture_stdout_with_utc_timestamps_and_redact_tokens(self):
        token = "123456789:abcDef_GHIjklMNopQRstuVWXyz012345"
        with tempfile.TemporaryDirectory() as directory:
            logger = run_bot.make_logger(directory, token)
            try:
                supervisor = run_bot.Supervisor(Mock(), logger, environ={})
                supervisor._capture(io.StringIO(
                    f"GET https://api.telegram.org/bot{token}/getUpdates\n"
                    f"token={token}\nnormal heartbeat\n"))
                output = Path(directory, "bot.log").read_text()
                self.assertNotIn(token, output)
                self.assertIn("[REDACTED]", output)
                self.assertIn("normal heartbeat", output)
                self.assertRegex(output, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO child")
                handler = logger.handlers[0]
                self.assertEqual(handler.maxBytes, 2 * 1024 * 1024)
                self.assertEqual(handler.backupCount, 3)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_unknown_telegram_token_and_long_lines_are_sanitized(self):
        formatter = run_bot.RedactingFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0,
                                   "https://api.telegram.org/bot987654321:unknown_value/getMe " + "x" * 20000,
                                   (), None)
        output = formatter.format(record)
        self.assertNotIn("unknown_value", output)
        self.assertLess(len(output), 16500)
        self.assertIn("[truncated]", output)


if __name__ == "__main__":
    unittest.main()
