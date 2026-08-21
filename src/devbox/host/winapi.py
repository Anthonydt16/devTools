"""Thin ``ctypes`` bindings to the Win32 APIs Devbox needs.

Scope is deliberately tiny — everything here is unavailable on Linux, so it is
the one module of the codebase that cannot be exercised by the cloud test suite.
Keeping it minimal keeps the untested surface minimal.

Two capabilities are provided:

**Process identity** (:func:`process_creation_time`, :func:`process_is_alive`)
    ``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION`` (0x1000).  That
    right is granted to a standard user for their own processes — no elevation,
    no ``SeDebugPrivilege``.

**Job objects** (:class:`JobObject`)
    A Windows Job Object is the only reliable way to kill a *process tree* on
    Windows: there is no ``kill(-pgid)``.  ``npm run dev`` spawns ``node`` which
    spawns bundlers and watchers; terminating only the ``npm`` PID orphans all of
    them.  Devbox creates one job per supervised service with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so that the tree also dies if Devbox
    itself is killed, and calls ``TerminateJobObject`` for an explicit stop.

    Since Windows 8 jobs can nest, so assigning a process that already belongs to
    another job (a CI agent's job, for instance) succeeds instead of failing with
    ``ERROR_ACCESS_DENIED``.

Every function raises :class:`~devbox.errors.UnsupportedPlatformError` when
called off Windows rather than failing at import time, so this module can be
imported anywhere.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Final, cast

from devbox.errors import ProcessError, UnsupportedPlatformError

__all__ = [
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "JobObject",
    "process_creation_time",
    "process_is_alive",
]

# -- access rights -----------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
PROCESS_TERMINATE: Final = 0x0001
PROCESS_SET_QUOTA: Final = 0x0100

# -- job object -------------------------------------------------------------
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9

STILL_ACTIVE: Final = 259
ERROR_ACCESS_DENIED: Final = 5
ERROR_INVALID_PARAMETER: Final = 87

# FILETIME counts 100-nanosecond intervals since 1601-01-01 UTC; this is the
# offset to the POSIX epoch in the same unit.
_FILETIME_EPOCH_DELTA: Final = 116_444_736_000_000_000
_FILETIME_TICKS_PER_SECOND: Final = 10_000_000


def _require_windows(operation: str) -> None:
    if sys.platform != "win32":
        msg = f"{operation} requires Windows (running on {sys.platform!r})"
        raise UnsupportedPlatformError(
            msg,
            hint="This code path is Windows-only by design; see docs/windows.md.",
        )


def _kernel32() -> Any:
    """Return ``kernel32`` with ``last_error`` propagation enabled."""
    _require_windows("Win32 access")
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


class _FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32))

    def as_posix_timestamp(self) -> float:
        raw = (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)
        return (raw - _FILETIME_EPOCH_DELTA) / _FILETIME_TICKS_PER_SECOND


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def process_creation_time(pid: int) -> float | None:
    """POSIX timestamp at which *pid* was created, or ``None`` if unreadable.

    ``None`` means "cannot verify ownership" — never "the process is gone".
    """
    _require_windows("process_creation_time")
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return creation.as_posix_timestamp()
    finally:
        kernel32.CloseHandle(handle)


def process_is_alive(pid: int) -> bool:
    """True when *pid* names a process that has not yet exited."""
    _require_windows("process_is_alive")
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED means the process exists but belongs to somebody
        # else; that is still "alive", and Devbox will refuse to touch it because
        # the creation time will not match any record it owns.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class JobObject:
    """An owned Windows Job Object used to supervise one process tree.

    Usage::

        job = JobObject.create("devbox-backend")
        proc = subprocess.Popen(argv, ...)
        job.assign_pid(proc.pid)
        ...
        job.terminate()   # kills the process *and every descendant*
        job.close()

    The window between ``CreateProcess`` and ``AssignProcessToJobObject`` is a
    known, unavoidable race with plain ``subprocess.Popen``: a child spawned in
    those microseconds would not inherit the job.  In practice interpreters and
    package managers have not finished start-up by then.  Documented in
    docs/windows.md, together with the ``CREATE_SUSPENDED`` alternative and why
    it is not used (``Popen`` does not expose the primary thread handle needed to
    resume the process).
    """

    __slots__ = ("_closed", "_handle", "_kernel32", "name")

    def __init__(self, handle: int, name: str, kernel32: Any) -> None:
        self._handle = handle
        self.name = name
        self._kernel32 = kernel32
        self._closed = False

    @classmethod
    def create(cls, name: str, *, kill_on_close: bool = True) -> JobObject:
        """Create an unnamed job object configured to kill its tree on close.

        *name* is only used in error messages: passing a real Win32 object name
        would let an unrelated process on the same desktop open the job.
        """
        _require_windows("JobObject.create")
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _win32_error(f"CreateJobObjectW failed for job {name!r}")
        job = cls(handle=int(handle), name=name, kernel32=kernel32)
        if kill_on_close:
            job._set_kill_on_close()
        return job

    def _set_kill_on_close(self) -> None:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise _win32_error(f"SetInformationJobObject failed for job {self.name!r}")

    def assign_pid(self, pid: int) -> None:
        """Put *pid* — and therefore its future descendants — inside this job."""
        _require_windows("JobObject.assign_pid")
        handle = self._kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            raise _win32_error(f"OpenProcess failed for pid {pid}")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, handle):
                raise _win32_error(f"AssignProcessToJobObject failed for pid {pid}")
        finally:
            self._kernel32.CloseHandle(handle)

    def terminate(self, exit_code: int = 1) -> None:
        """Terminate every process in the job.  Safe to call more than once."""
        if self._closed:
            return
        _require_windows("JobObject.terminate")
        if not self._kernel32.TerminateJobObject(self._handle, exit_code):
            # The job may already be empty; that is not an error worth raising.
            last = ctypes.get_last_error()
            if last not in (0, ERROR_INVALID_PARAMETER):
                raise _win32_error(f"TerminateJobObject failed for job {self.name!r}")

    def close(self) -> None:
        """Release the handle.  With ``kill_on_close`` this also kills the tree."""
        if self._closed:
            return
        self._closed = True
        self._kernel32.CloseHandle(self._handle)

    def __enter__(self) -> JobObject:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _win32_error(operation: str) -> ProcessError:
    code = ctypes.get_last_error()
    message = ctypes.FormatError(code).strip()  # type: ignore[attr-defined]
    return ProcessError(f"{operation}: [{code}] {cast(str, message)}")
