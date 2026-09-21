"""The ephemeral cross-process mutex both coordination backends take.

Two kinds of mutex exist in this design and they are never conflated:

- **This one is ephemeral and per operation.** It is an advisory OS lock on a file, the
  kernel releases it when its holder exits however it exits, and it is held for one commit
  or one file operation -- never for a ticket's duration. Nothing about it is a token: the
  lock *file* is a rendezvous point, so its existence says nothing at all, which is why
  process death needs no staleness heuristic to recover from.
- **A durable `claim` is the other kind**, and only a lifecycle command releases it.

Both backends take the same lock for the same reason, which is why it lives here rather
than once per backend. A backend whose records are its *only* shared state (SQLite: one
database file, real transactions) still needs it around a **file operation**, because such
an operation is not one transaction: it verifies the claim and the read token, commits the
intent, changes the project's bytes, and finalises the receipt. Two processes that verified
before either committed would both apply their change, which is exactly what "one read token
authorises one mutation" exists to prevent.

A caller that cannot take the lock within `LOCK_TIMEOUT` is refused with `Busy` -- a
structured answer, exit 4, nothing written -- rather than a command that hangs.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..errors import Busy

#: How long a caller waits for the store's lock before it is told to come back later. Short
#: on purpose: the lock is held for one operation, so a wait this long means somebody else is
#: mid-operation rather than busy for hours, and an agent's correct response is to pick other
#: work rather than to sit in a queue.
LOCK_TIMEOUT = 2.0

#: How often a waiter re-tries while it waits. Small enough that a fast operation hands the
#: lock over promptly, large enough that the polling itself is not the cost.
LOCK_POLL_SECONDS = 0.01

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def _try_lock(handle) -> bool:
    """Take an exclusive advisory lock on `handle`, or report that somebody holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    import msvcrt  # pragma: no cover - Windows

    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _release_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt  # pragma: no cover - Windows

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class StoreLock:
    """One store's lock file, and the context manager that holds it.

    Re-entrant per instance, because an operation nests a commit inside itself and the two
    must be one critical section: an flock is per open handle, so taking a second one in the
    same process would block on the lock that process already holds. `describe` is what the
    refusals name -- the store's root, so a caller is told *which* store is busy."""

    def __init__(self, path, describe=None, limits=None):
        self._path = Path(path)
        self._describe = str(describe if describe is not None else path)
        #: Called for `(timeout, poll)` each time the lock is taken, so a backend whose
        #: timeouts are module constants -- and whose tests patch those constants -- keeps
        #: that seam instead of freezing the values at construction.
        self._limits = limits if limits is not None else (lambda: (LOCK_TIMEOUT, LOCK_POLL_SECONDS))
        #: Guards this instance against another thread in the same process, so the flock's
        #: holder is always exactly one thread.
        self._mutex = threading.Lock()
        #: How deep the current thread is inside the lock (0 = not held), and the handle it
        #: holds, so a nested hold does not open a second one.
        self._depth = 0
        self._handle = None

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def hold(self):
        """Hold the lock for the length of one unit of work, or refuse with `Busy`."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        timeout, poll = self._limits()
        if not self._mutex.acquire(timeout=timeout):
            raise Busy(
                f"another thread in this process is committing to {self._describe}; "
                "nothing was written",
                reason="store_locked",
            )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._path, "a+b")
            if handle.tell() == 0:
                handle.write(b"\n")  # a byte for a platform whose lock needs one
                handle.flush()
            try:
                deadline = time.monotonic() + timeout
                while not _try_lock(handle):
                    if time.monotonic() >= deadline:
                        raise Busy(
                            f"another arbite process is committing to {self._describe}; "
                            "nothing was written",
                            reason="store_locked",
                        )
                    time.sleep(poll)
            except BaseException:
                handle.close()
                raise
            self._depth = 1
            self._handle = handle
            try:
                yield
            finally:
                self._depth = 0
                self._handle = None
                try:
                    _release_lock(handle)
                finally:
                    handle.close()
        finally:
            self._mutex.release()
