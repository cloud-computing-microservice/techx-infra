"""Bounded POSIX subprocess supervision for a freshly owned worker task.

Only trusted static adapters may supply argv/cwd/environment. This is not a
payload-to-shell adapter or operation approval. It never publishes completion,
transfers ownership or retries a stage. R04 must still terminate the original
VM before replacement: process groups cannot contain deliberately detached code
or survive loss of this supervisor/host.
"""
from datetime import timezone
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

from execution_v2 import timestamp
from worker_task_v2 import ActiveTaskLease


class ProcessRejected(ValueError):
    """No success receipt is permitted; retain evidence and request cleanup."""


class BoundProcess:
    """One process group, one stage attempt, no restart after uncertain outcome."""
    def __init__(self, active, *, check_interval=2.0, check_timeout=10.0,
                 poll_interval=0.05, terminate_grace=2.0):
        if not isinstance(active, ActiveTaskLease):
            raise ValueError("Fresh typed task ownership required")
        for value in (check_interval, check_timeout, poll_interval, terminate_grace):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 10:
                raise ValueError("Finite bounded supervision intervals required")
        self._active = active
        self._interval, self._timeout = check_interval, check_timeout
        self._poll, self._grace = poll_interval, terminate_grace
        self._used = False

    def run(self, argv, *, cwd, log_path, environment):
        with self._active._process_lock:
            if self._used or self._active._process_consumed:
                raise ProcessRejected("STAGE_ATTEMPT_ALREADY_CONSUMED")
            self._used = True
            self._active._process_consumed = True
        if os.name != "posix":
            raise ProcessRejected("POSIX_PROCESS_GROUP_REQUIRED")
        if (not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv)
                or not Path(argv[0]).is_absolute() or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
                or not Path(log_path).is_absolute() or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not k or "=" in k or "\0" in k or not isinstance(v, str) or "\0" in v
                       for k, v in environment.items())):
            raise ValueError("Trusted absolute command/working directory/private log and explicit environment required")
        # Start the monotonic budget BEFORE any network guard. A hung SDK read
        # never extends the task/cleanup deadline, including initial admission.
        anchor = time.monotonic()
        tasks = self._active._runtime._tasks
        task = self._active._observed._envelope["task"]
        wall = tasks._clock().astimezone(timezone.utc)
        deadline = min(timestamp(task["expiresAt"]), timestamp(tasks._store.execution["cleanupStartAt"]))
        end = anchor + (deadline - wall).total_seconds()
        responses = queue.Queue(maxsize=1)
        pending = None
        next_check = anchor
        process = None

        def start_check():
            def check():
                try:
                    self._active.require_current()
                    responses.put(True)
                except BaseException:
                    # No exception text/private API response enters logs.
                    responses.put(False)
            threading.Thread(target=check, daemon=True, name="techx-task-guard").start()

        def check_state(*, initial=False):
            nonlocal pending, next_check
            now = time.monotonic()
            if now >= end:
                raise ProcessRejected("TASK_OR_CLEANUP_DEADLINE_REACHED")
            if pending is not None:
                # A response arriving after the bound does not revive authority.
                if now - pending >= self._timeout:
                    raise ProcessRejected("TASK_GUARD_OBSERVATION_TIMEOUT")
                try:
                    valid = responses.get_nowait()
                except queue.Empty:
                    return False
                pending = None
                if not valid:
                    raise ProcessRejected("TASK_OWNERSHIP_OR_IDENTITY_REJECTED")
                next_check = now + self._interval
                return True
            if now >= next_check or initial:
                pending = now
                start_check()
                return False
            return True

        def stop_group():
            if process is None:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Always escalate to the WHOLE original group, even if its leader
            # exited on TERM while a descendant ignored it.
            until = min(time.monotonic() + self._grace, end)
            while time.monotonic() < until:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(self._poll)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise ProcessRejected("PROCESS_TERMINATION_UNCONFIRMED") from None

        try:
            while not check_state(initial=True):
                time.sleep(self._poll)
            if time.monotonic() >= end:
                raise ProcessRejected("TASK_OR_CLEANUP_DEADLINE_REACHED")
            # O_EXCL prevents reuse/symlink replacement and chmod occurs before
            # the child can emit credentials or private tool output.
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                if time.monotonic() >= end:
                    raise ProcessRejected("TASK_OR_CLEANUP_DEADLINE_REACHED")
                process = subprocess.Popen(list(argv), cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, shell=False, close_fds=True, start_new_session=True)
                while True:
                    current = check_state()
                    code = process.poll()
                    if code is not None:
                        if code != 0:
                            raise ProcessRejected("STAGE_PROCESS_FAILED")
                        # A zero leader exit is insufficient if descendants are
                        # still present. Never publish success for such a stage.
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise ProcessRejected("UNEXPECTED_STAGE_DESCENDANTS")
                        # Drain an older in-flight check, then force a read
                        # initiated AFTER actual process exit.
                        while pending is not None:
                            check_state()
                            if pending is not None:
                                time.sleep(self._poll)
                        next_check = 0
                        while not check_state():
                            time.sleep(self._poll)
                        return dict(returnCode=0, businessAcceptance="NOT_ESTABLISHED")
                    time.sleep(self._poll)
        finally:
            stop_group()
