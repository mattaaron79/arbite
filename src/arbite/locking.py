"""A small, coarse, process-safe operation lock (planning key C05).

The workspace and the sink are separate durability domains, and neither the file
sink's record journal nor SQLite's transactions can serialize a *filesystem*
operation against a concurrent lifecycle transition. A mutation commits its
intent, then changes bytes on disk, then finalizes its receipt -- and a `close`
that ran between the first and last steps would be reading a half-applied world.

This module is the deliberately small answer: one coarse lock, held only for the
duration of a single operation (never for an agent's whole ticket duration),
whose whole implementation is a `flock` on a lock file. Two properties matter:

- **Process death cannot leave it held.** `flock` is owned by the open file
  description and is released by the operating system when the process exits for
  any reason, so there is no "stale lock" to time out and no need to invent an
  agent-staleness policy. This is exactly the requirement the plan states.
- **It is re-entrant within one process.** A nested acquisition (a mutation that
  calls a lifecycle helper which also wants the lock, or a test that holds the
  lock and then drives an engine) increments a depth counter rather than
  deadlocking on a second `flock` of the same file from the same process.

The lock is *not* the coordination transaction lock the sinks already use; it is
a separate file. That is deliberate: the file sink's record journal already takes
its own `flock`, and acquiring the same file twice in one process would deadlock.
A separate file also means the engine can hold this lock across the
intent -> apply -> receipt sequence while each store transaction takes its own
lock in a fixed order (operation lock first, store lock second), so there is no
lock-ordering cycle.

Nothing here sleeps on a timer or runs in the background: `acquire` waits at most
`timeout` seconds with a short poll, then raises a retryable
`CoordinationConflict`. There is no daemon, no watcher and no automatic takeover.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .errors import CoordinationConflict, UnsupportedCoordination

try:  # pragma: no cover - the shipped platforms (Linux/macOS) all have fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

#: Default bound on how long an operation waits for the lock. Small enough that a
#: one-shot command stays prompt, long enough for ordinary contention to resolve.
DEFAULT_OPERATION_LOCK_TIMEOUT = 5.0

#: Sleep between acquisition attempts while waiting for the lock.
POLL_SECONDS = 0.01


@dataclass
class _Held:
    """The process-local state of one held lock file."""

    fd: int
    depth: int


#: Lock files this process currently holds, keyed by real path. The kernel's
#: `flock` is what excludes *other* processes; this registry is what makes a
#: second acquisition in this process re-entrant instead of a self-deadlock.
_HELD: Dict[str, _Held] = {}


def operation_lock_held(path) -> bool:
    """True when this process currently holds the operation lock at `path`."""
    return os.path.realpath(str(path)) in _HELD


class _NullLock:
    """A no-op lock for a store that has no cross-process operation locking.

    Deliberately silent-but-documented: a custom store that does not override
    `operation_lock_path()` gets no cross-process serialization of filesystem
    operations. The two shipped sinks both provide a real lock.
    """

    path = None

    def acquire(self, timeout: Optional[float] = None) -> "_NullLock":
        return self

    def release(self) -> None:
        return None

    def is_held(self) -> bool:
        return False

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


NULL_OPERATION_LOCK = _NullLock()


class OperationLock:
    """A coarse process-safe lock, re-entrant within the acquiring process."""

    def __init__(self, path, *, timeout: float = DEFAULT_OPERATION_LOCK_TIMEOUT):
        self.path = os.path.realpath(str(path))
        self.timeout = float(timeout)
        self._fd: Optional[int] = None
        self._depth = 0

    # -- acquisition -------------------------------------------------------

    def acquire(self, timeout: Optional[float] = None) -> "OperationLock":
        """Take the lock, waiting at most `timeout` seconds.

        Re-entrant: a second acquisition by this process (same lock file) just
        increments a depth counter and returns immediately. Otherwise the lock is
        taken with `flock`, which the operating system releases if this process
        dies -- so a crash can never leave the lock permanently held.
        """
        if fcntl is None:  # pragma: no cover - non-posix platform
            raise UnsupportedCoordination(
                "the operation lock requires a POSIX platform with fcntl; arbite "
                "cannot serialize filesystem operations here"
            )
        held = _HELD.get(self.path)
        if held is not None:
            held.depth += 1
            self._fd = held.fd
            self._depth = held.depth
            return self

        parent = os.path.dirname(self.path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:  # pragma: no cover - unwritable store dir
                raise UnsupportedCoordination(
                    f"could not create the operation lock directory {parent}: {e}"
                )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:  # pragma: no cover - unwritable store dir
            raise UnsupportedCoordination(
                f"could not open the operation lock {self.path}: {e}"
            )

        import time

        deadline = time.monotonic() + max(0.0, float(self.timeout if timeout is None else timeout))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise CoordinationConflict(
                        "another process holds the coordination operation lock; "
                        "retry shortly (a dead process cannot leave this lock held "
                        "-- the operating system releases it)",
                        details={
                            "lock": self.path,
                            "timeout_seconds": self.timeout if timeout is None else timeout,
                        },
                    )
                time.sleep(POLL_SECONDS)
        _HELD[self.path] = _Held(fd=fd, depth=1)
        self._fd = fd
        self._depth = 1
        return self

    # -- release -----------------------------------------------------------

    def release(self) -> None:
        if self._fd is None and self.path not in _HELD:
            return
        held = _HELD.get(self.path)
        if held is None:
            self._fd = None
            self._depth = 0
            return
        held.depth -= 1
        if held.depth > 0:
            self._depth = held.depth
            self._fd = None
            return
        _HELD.pop(self.path, None)
        self._fd = None
        self._depth = 0
        try:
            fcntl.flock(held.fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        try:
            os.close(held.fd)
        except OSError:  # pragma: no cover
            pass

    def is_held(self) -> bool:
        """True when this process currently holds this lock file."""
        return self.path in _HELD

    def __enter__(self) -> "OperationLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


__all__ = [
    "DEFAULT_OPERATION_LOCK_TIMEOUT",
    "NULL_OPERATION_LOCK",
    "OperationLock",
    "operation_lock_held",
]
