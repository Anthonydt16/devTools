"""Process identity: liveness and creation time.

PIDs are recycled.  Killing "the process with PID 8132" a day later can kill an
unrelated application, so Devbox pairs every recorded PID with the process
*creation time* and refuses to act when the two disagree.

Creation time is read from the OS:

* Windows — ``GetProcessTimes`` on a handle opened with
  ``PROCESS_QUERY_LIMITED_INFORMATION`` (available to a standard user for their
  own processes, no elevation required);
* Linux — field 22 (``starttime``) of ``/proc/<pid>/stat``, in clock ticks since
  boot;
* macOS / other — ``ps -o lstart=``, parsed to a POSIX timestamp.

The unit differs per platform on purpose: the value is only ever compared with
another value produced by the *same* platform, so it is treated as an opaque
token.  :func:`process_creation_time` returns ``None`` when the answer cannot be
obtained, and callers must treat ``None`` as "ownership unverifiable".
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ProcessIdentity", "process_creation_time", "process_is_alive"]


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A PID paired with the creation time that makes it unambiguous."""

    pid: int
    creation_time: float | None

    @classmethod
    def of(cls, pid: int) -> ProcessIdentity:
        return cls(pid=pid, creation_time=process_creation_time(pid))

    def matches_live_process(self) -> bool:
        """True when a live process with this PID *and* this creation time exists.

        Returns ``False`` when the PID is dead, and ``False`` when the PID is
        alive but was created at a different time (i.e. it was recycled).

        When the recorded creation time is ``None`` — an older state file, or a
        platform that could not answer — we fall back to liveness alone and the
        caller is expected to refuse destructive actions; see
        :meth:`ownership_is_verifiable`.
        """
        if not process_is_alive(self.pid):
            return False
        if self.creation_time is None:
            return True
        current = process_creation_time(self.pid)
        if current is None:
            return True
        return _creation_times_equal(self.creation_time, current)

    @property
    def ownership_is_verifiable(self) -> bool:
        """False when we never managed to record a creation time for this PID."""
        return self.creation_time is not None


def _creation_times_equal(left: float, right: float) -> bool:
    """Compare creation times with a tolerance covering clock-tick rounding.

    Linux reports ``starttime`` in clock ticks (usually 100 Hz), so a value that
    round-trips through JSON can differ in the last decimal.  10 ms is far below
    the time it takes for a PID to be recycled.
    """
    return abs(left - right) < 0.01


def process_is_alive(pid: int) -> bool:
    """True when a process with this PID currently exists.

    Says nothing about *which* process it is — always pair it with the creation
    time via :meth:`ProcessIdentity.matches_live_process`.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, it just is not ours.  Devbox will refuse to touch it later.
        return True
    except OSError:
        return False
    return True


def process_creation_time(pid: int) -> float | None:
    """Opaque, platform-local creation timestamp, or ``None`` if unavailable."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_creation_time(pid)
    if sys.platform.startswith("linux"):
        return _linux_creation_time(pid)
    return _ps_creation_time(pid)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
def _linux_creation_time(pid: int) -> float | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The comm field (2) is parenthesised and may itself contain spaces and
    # parentheses, so split on the *last* ')' rather than tokenising blindly.
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2 :].split()
    # After comm, field indices shift by 2: starttime is field 22 -> index 19.
    if len(fields) <= 19:
        return None
    try:
        ticks = float(fields[19])
    except ValueError:
        return None
    hertz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return ticks / float(hertz or 100)


# ---------------------------------------------------------------------------
# macOS / generic POSIX fallback
# ---------------------------------------------------------------------------
def _ps_creation_time(pid: int) -> float | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        # ``ps`` formats vary; a stable string hash is still a usable token
        # because it is only ever compared with another reading of the same PID.
        return float(abs(hash(value)) % 10**9)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _windows_creation_time(pid: int) -> float | None:
    from devbox.host import winapi

    return winapi.process_creation_time(pid)


def _windows_process_is_alive(pid: int) -> bool:
    from devbox.host import winapi

    return winapi.process_is_alive(pid)
