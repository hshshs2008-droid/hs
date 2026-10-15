"""Supervise bot.py without changing its SQLite state or requiring services.

Run ``python3 -u run_bot.py``; inspect with ``--status`` and stop with ``--stop``.
The lock and rotating log live beside ALERT_STATE_PATH, or in project/.state.
All unexpected child exits, including exit 0, are retried with bounded backoff.
This supervisor still needs its hosting environment to remain running.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time


PROJECT = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve()
BOT_SCRIPT = PROJECT / "bot.py"
LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
INITIAL_BACKOFF = 5
MAX_BACKOFF = 60
STABLE_SECONDS = 300
# bot.py can join its market and history workers for 25 seconds each.
STOP_TIMEOUT = 60
KILL_TIMEOUT = 5


def runtime_directory(environ=None):
    """Share the existing database directory; never move or recreate the DB."""
    environ = os.environ if environ is None else environ
    configured = environ.get("ALERT_STATE_PATH")
    if configured and configured != ":memory:":
        return Path(configured).expanduser().resolve().parent
    return PROJECT / ".state"


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _start_ticks(pid):
    """Linux process start time distinguishes a live owner from a reused PID."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        path = Path(f"/proc/{pid}")
        if path.stat().st_uid != os.getuid():
            return None
        fields = (path / "stat").read_text().rpartition(")")[2].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _script_matches(pid, expected):
    try:
        proc = Path(f"/proc/{pid}")
        arguments = (proc / "cmdline").read_bytes().split(b"\0")
        relative = []
        for argument in arguments[1:]:
            if not argument or argument.startswith(b"-"):
                continue
            candidate = Path(os.fsdecode(argument))
            # Some hosts allow cmdline but deny another process's cwd symlink.
            # Our launcher uses absolute script arguments, so verify those first.
            if candidate.is_absolute() and candidate.resolve() == expected:
                return True
            if not candidate.is_absolute():
                relative.append(candidate)
        if relative:
            cwd = (proc / "cwd").resolve(strict=True)
            return any((cwd / candidate).resolve() == expected for candidate in relative)
    except (OSError, ValueError):
        pass
    return False


class AlreadyRunning(Exception):
    """Another supervisor owns this state directory's lock."""


class OrphanedChild(Exception):
    """A verified child survived an older supervisor without inheriting its lock."""


def _read_metadata(lock_file):
    try:
        lock_file.seek(0)
        metadata = json.loads(lock_file.read(8192))
        return metadata if isinstance(metadata, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _verified_child(metadata):
    child = metadata.get("child_pid")
    ticks = metadata.get("child_start_ticks")
    return bool(ticks is not None and _start_ticks(child) == ticks
                and _script_matches(child, BOT_SCRIPT))


class SupervisorLock:
    """Keep an advisory lock for the whole lifetime, including restart delays."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "bot.lock"
        self.file = None
        self.metadata = {}

    def acquire(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        self.file = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise AlreadyRunning() from None
        # Older versions did not pass the flock to bot.py. Do not overwrite the
        # only verified identity of a surviving child before checking for it.
        if _verified_child(_read_metadata(self.file)):
            self.file.close()
            self.file = None
            raise OrphanedChild() from None
        self.metadata = {
            "supervisor_pid": os.getpid(),
            "supervisor_start_ticks": _start_ticks(os.getpid()),
            "script_path": str(SCRIPT),
            "started_at": _utc_now(),
            "child_pid": None,
            "child_start_ticks": None,
        }
        self.update()
        return self

    def update(self, **fields):
        self.metadata.update(fields)
        self.metadata["updated_at"] = _utc_now()
        self.file.seek(0)
        json.dump(self.metadata, self.file, sort_keys=True)
        self.file.truncate()
        self.file.flush()

    def close(self):
        if self.file is not None:
            try:
                # run() clears a reaped child. Preserve identity if a failure
                # leaves one alive so --status/--stop can recover it safely.
                self.update(stopped_at=_utc_now())
            finally:
                self.file.close()
                self.file = None


def supervisor_status(directory):
    """Inspect without creating files, opening SQLite, or starting anything."""
    result = {"lock_held": False, "supervisor_pid": None,
              "supervisor_alive": False, "child_pid": None, "child_alive": False,
              "orphan_child_alive": False, "unidentified_lock_holder": False}
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(Path(directory) / "bot.lock", flags)
    except FileNotFoundError:
        return result
    with os.fdopen(descriptor, "r", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["lock_held"] = True
        metadata = _read_metadata(lock_file)
    pid = metadata.get("supervisor_pid")
    ticks = metadata.get("supervisor_start_ticks")
    verified = (result["lock_held"] and ticks is not None
                and metadata.get("script_path") == str(SCRIPT)
                and _start_ticks(pid) == ticks and _script_matches(pid, SCRIPT))
    result.update(supervisor_pid=pid, supervisor_alive=bool(verified),
                  supervisor_start_ticks=ticks,
                  started_at=metadata.get("started_at"), updated_at=metadata.get("updated_at"))
    child = metadata.get("child_pid")
    child_ticks = metadata.get("child_start_ticks")
    child_verified = _verified_child(metadata)
    result.update(child_pid=child, child_start_ticks=child_ticks,
                  child_alive=child_verified, orphan_child_alive=bool(child_verified and not verified),
                  unidentified_lock_holder=bool(result["lock_held"] and not verified and not child_verified))
    return result


def stop_supervisor(directory):
    """Stop a verified supervisor, or SIGINT its verified orphan; never guess a PID."""
    status = supervisor_status(directory)
    if status["supervisor_alive"]:
        pid = status["supervisor_pid"]
        ticks = status["supervisor_start_ticks"]
        expected_script = SCRIPT
        stop_signal = signal.SIGTERM
    elif status.get("orphan_child_alive"):
        pid = status["child_pid"]
        ticks = status["child_start_ticks"]
        expected_script = BOT_SCRIPT
        stop_signal = signal.SIGINT
    else:
        return False
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        try:
            # Recheck after pinning the process to close the PID-reuse race.
            if (_start_ticks(pid) != ticks or not _script_matches(pid, expected_script)):
                return False
            signal.pidfd_send_signal(descriptor, stop_signal)
        finally:
            os.close(descriptor)
    else:
        if (_start_ticks(pid) != ticks or not _script_matches(pid, expected_script)):
            return False
        os.kill(pid, stop_signal)
    return True


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime
    _bot_url = re.compile(r"(?i)(/bot)[^/\s?#]+")
    _token = re.compile(r"\b[0-9]{5,}:[A-Za-z0-9_-]{20,}\b")

    def __init__(self, token=""):
        super().__init__("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.token = token

    def format(self, record):
        rendered = super().format(record)
        if self.token:
            rendered = rendered.replace(self.token, "[REDACTED]")
        rendered = self._bot_url.sub(r"\1[REDACTED]", rendered)
        rendered = self._token.sub("[REDACTED]", rendered)
        # A corrupt child cannot turn one giant output line into an unbounded log.
        if len(rendered) > 16384:
            rendered = rendered[:16384] + " [truncated]"
        return rendered


def make_logger(directory, token=""):
    logger = logging.getLogger(f"telegram_bot.supervisor.{os.getpid()}.{id(directory)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(Path(directory) / "bot.log", maxBytes=LOG_BYTES,
                                  backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(RedactingFormatter(token))
    logger.addHandler(handler)
    return logger


class Supervisor:
    def __init__(self, lock, logger, *, popen=subprocess.Popen, clock=time.monotonic,
                 stop_event=None, environ=None):
        self.lock = lock
        self.logger = logger
        self.popen = popen
        self.clock = clock
        self.stop = stop_event if stop_event is not None else threading.Event()
        self.environ = dict(os.environ if environ is None else environ)
        # Resolve a configured relative SQLite path before fixing the child cwd.
        database = self.environ.get("ALERT_STATE_PATH")
        if database and database != ":memory:":
            self.environ["ALERT_STATE_PATH"] = str(Path(database).expanduser().resolve())
        self.environ["PYTHONUNBUFFERED"] = "1"
        self.child = None
        self.stop_signal = signal.SIGTERM

    def request_stop(self, signum=signal.SIGTERM, _frame=None):
        self.stop_signal = signum
        self.stop.set()

    def _capture(self, stream):
        try:
            for line in stream:
                self.logger.info("child %s", line.rstrip("\r\n"))
        except (OSError, ValueError) as error:
            self.logger.warning("child output unavailable error_type=%s", type(error).__name__)
        finally:
            stream.close()

    def _stop_child(self):
        if self.child is None:
            return
        if self.child.poll() is None:
            # bot.py handles KeyboardInterrupt by stopping/joining its workers
            # and closing SQLite. SIGINT also gives supervisor SIGTERM a graceful
            # child shutdown instead of Python's default abrupt SIGTERM exit.
            child_signal = signal.SIGINT
            self.logger.info("forwarding stop received_signal=%s child_signal=%s child_pid=%s",
                             int(self.stop_signal), int(child_signal), self.child.pid)
            try:
                self.child.send_signal(child_signal)
            except ProcessLookupError:
                pass
            try:
                self.child.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.logger.warning("child exceeded graceful stop; killing child_pid=%s", self.child.pid)
                self.child.kill()
                self.child.wait(timeout=KILL_TIMEOUT)

    def run(self):
        delay = INITIAL_BACKOFF
        self.logger.info("supervisor started supervisor_pid=%s", os.getpid())
        while not self.stop.is_set():
            started = self.clock()
            reader = None
            try:
                self.child = self.popen(
                    [sys.executable, "-u", str(BOT_SCRIPT)],
                    cwd=str(PROJECT), env=self.environ, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    start_new_session=True,
                    # The child keeps the same flock if this supervisor is
                    # SIGKILLed, including the gap before metadata is updated.
                    pass_fds=(self.lock.file.fileno(),),
                )
                self.lock.update(child_pid=self.child.pid,
                                 child_start_ticks=_start_ticks(self.child.pid))
                self.logger.info("child started child_pid=%s", self.child.pid)
                reader = threading.Thread(target=self._capture, args=(self.child.stdout,),
                                          name="bot-log", daemon=True)
                reader.start()
                while self.child.poll() is None and not self.stop.is_set():
                    self.stop.wait(0.25)
                if self.stop.is_set():
                    self._stop_child()
                    break
                exit_code = self.child.wait()
                self.logger.warning("unexpected child exit child_pid=%s exit_code=%s",
                                    self.child.pid, exit_code)
            except Exception as error:
                self.logger.error("child launch or lifecycle failed error_type=%s", type(error).__name__)
                self._stop_child()
            finally:
                if reader is not None:
                    reader.join(timeout=1)
                if self.child is None or self.child.poll() is not None:
                    self.lock.update(child_pid=None, child_start_ticks=None)
                self.child = None
            if self.stop.is_set():
                break
            if self.clock() - started >= STABLE_SECONDS:
                delay = INITIAL_BACKOFF
            self.logger.info("restart scheduled delay_seconds=%s", delay)
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, MAX_BACKOFF)
        self.logger.info("supervisor stopped intentionally")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="Read supervisor/child status without starting them")
    group.add_argument("--stop", action="store_true", help="Stop a verified supervisor or its verified orphan bot")
    arguments = parser.parse_args(argv)
    directory = runtime_directory()
    if arguments.status:
        print(json.dumps(supervisor_status(directory), sort_keys=True))
        return 0
    if arguments.stop:
        stopped = stop_supervisor(directory)
        print("Stop requested; wait for --status to report no live child before starting again."
              if stopped else "No verified supervisor or orphan found; unknown lock holders are not signalled.")
        return 0 if stopped else 1
    lock = SupervisorLock(directory)
    logger = None
    previous_handlers = {}
    try:
        lock.acquire()
        logger = make_logger(directory, os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        supervisor = Supervisor(lock, logger)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, supervisor.request_stop)
        return supervisor.run()
    except AlreadyRunning:
        print("A supervisor or inherited child holds the lock. Inspect --status; use --stop for a verified owner.",
              file=sys.stderr)
        return 2
    except OrphanedChild:
        print("A verified orphan bot is still running. Use --stop, wait for --status to show child_alive=false, then restart.",
              file=sys.stderr)
        return 2
    except Exception as error:
        if logger is not None:
            logger.error("supervisor failure error_type=%s", type(error).__name__)
        print(f"Supervisor failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        lock.close()
        if logger is not None:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
