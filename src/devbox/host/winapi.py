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
from typing import Any, Final

from devbox.errors import ProcessError, UnsupportedPlatformError
from devbox.host.platform_info import is_windows

__all__ = [
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "CapturedProcess",
    "JobObject",
    "capture_descendant_processes",
    "process_creation_time",
    "process_is_alive",
    "resume_process_threads",
]

# -- access rights -----------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
PROCESS_TERMINATE: Final = 0x0001
PROCESS_SET_QUOTA: Final = 0x0100
THREAD_SUSPEND_RESUME: Final = 0x0002

# -- toolhelp thread snapshots ----------------------------------------------
TH32CS_SNAPTHREAD: Final = 0x00000004
TH32CS_SNAPPROCESS: Final = 0x00000002
ERROR_NO_MORE_FILES: Final = 18
_RESUME_THREAD_FAILED: Final = 0xFFFFFFFF
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value

# -- job object -------------------------------------------------------------
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
JOB_OBJECT_TERMINATE: Final = 0x0008
JOB_OBJECT_QUERY: Final = 0x0004
SYNCHRONIZE: Final = 0x00100000
JOB_OBJECT_REOPEN_ACCESS: Final = JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY | SYNCHRONIZE

STILL_ACTIVE: Final = 259
ERROR_ACCESS_DENIED: Final = 5
ERROR_INVALID_PARAMETER: Final = 87

# FILETIME counts 100-nanosecond intervals since 1601-01-01 UTC; this is the
# offset to the POSIX epoch in the same unit.
_FILETIME_EPOCH_DELTA: Final = 116_444_736_000_000_000
_FILETIME_TICKS_PER_SECOND: Final = 10_000_000


def _require_windows(operation: str) -> None:
    if not is_windows():
        msg = f"{operation} requires Windows (running on {sys.platform!r})"
        raise UnsupportedPlatformError(
            msg,
            hint="This code path is Windows-only by design; see docs/windows.md.",
        )


def _last_error() -> int:
    """``GetLastError`` for the current thread.

    Wrapped because typeshed declares ``ctypes.get_last_error`` Windows-only, so
    a type checker running on Linux cannot see it; the one ignore lives here
    instead of at every call site.
    """
    return int(ctypes.get_last_error())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------
# Declaring argtypes and restype is not optional on 64-bit Windows.  Without
# them ctypes assumes ``restype = c_int`` — 32 bits — while HANDLE is a 64-bit
# pointer, so every handle returned by OpenProcess, CreateJobObjectW and
# OpenJobObjectW is silently truncated and sign-extended.  Handle values are
# usually small enough to survive that by luck, which is exactly what makes the
# bug expensive: it works on the developer's machine and fails on somebody
# else's.  The same applies on the way in, where a handle passed as a C int
# loses its upper half.
#
# HANDLE is declared as c_void_p so a NULL return arrives as None and
# ``if not handle`` is correct for both success and failure.

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_LPCWSTR = ctypes.c_wchar_p
_LPVOID = ctypes.c_void_p

SIGNATURES: Final[dict[str, tuple[Any, tuple[Any, ...]]]] = {
    # HANDLE OpenProcess(DWORD access, BOOL inherit, DWORD pid)
    "OpenProcess": (_HANDLE, (_DWORD, _BOOL, _DWORD)),
    # BOOL GetProcessTimes(HANDLE, LPFILETIME x4)
    "GetProcessTimes": (_BOOL, (_HANDLE, _LPVOID, _LPVOID, _LPVOID, _LPVOID)),
    # BOOL GetExitCodeProcess(HANDLE, LPDWORD)
    "GetExitCodeProcess": (_BOOL, (_HANDLE, _LPVOID)),
    # BOOL CloseHandle(HANDLE)
    "CloseHandle": (_BOOL, (_HANDLE,)),
    # HANDLE CreateJobObjectW(LPSECURITY_ATTRIBUTES, LPCWSTR name)
    "CreateJobObjectW": (_HANDLE, (_LPVOID, _LPCWSTR)),
    # HANDLE OpenJobObjectW(DWORD access, BOOL inherit, LPCWSTR name)
    "OpenJobObjectW": (_HANDLE, (_DWORD, _BOOL, _LPCWSTR)),
    # BOOL SetInformationJobObject(HANDLE, JOBOBJECTINFOCLASS, LPVOID, DWORD)
    "SetInformationJobObject": (_BOOL, (_HANDLE, _DWORD, _LPVOID, _DWORD)),
    # BOOL AssignProcessToJobObject(HANDLE job, HANDLE process)
    "AssignProcessToJobObject": (_BOOL, (_HANDLE, _HANDLE)),
    # BOOL TerminateJobObject(HANDLE, UINT exit_code)
    "TerminateJobObject": (_BOOL, (_HANDLE, _DWORD)),
    # HANDLE CreateToolhelp32Snapshot(DWORD flags, DWORD process_id)
    "CreateToolhelp32Snapshot": (_HANDLE, (_DWORD, _DWORD)),
    # BOOL Thread32First(HANDLE snapshot, LPTHREADENTRY32 entry)
    "Thread32First": (_BOOL, (_HANDLE, _LPVOID)),
    # BOOL Thread32Next(HANDLE snapshot, LPTHREADENTRY32 entry)
    "Thread32Next": (_BOOL, (_HANDLE, _LPVOID)),
    # BOOL Process32FirstW(HANDLE snapshot, LPPROCESSENTRY32W entry)
    "Process32FirstW": (_BOOL, (_HANDLE, _LPVOID)),
    # BOOL Process32NextW(HANDLE snapshot, LPPROCESSENTRY32W entry)
    "Process32NextW": (_BOOL, (_HANDLE, _LPVOID)),
    # HANDLE OpenThread(DWORD access, BOOL inherit, DWORD thread_id)
    "OpenThread": (_HANDLE, (_DWORD, _BOOL, _DWORD)),
    # DWORD ResumeThread(HANDLE thread)
    "ResumeThread": (_DWORD, (_HANDLE,)),
    # BOOL TerminateProcess(HANDLE process, UINT exit_code)
    "TerminateProcess": (_BOOL, (_HANDLE, _DWORD)),
}
"""Every kernel32 function Devbox calls, with its exact C signature.

``tests/unit/test_winapi_signatures.py`` parses this module and asserts that the
set of functions actually called matches this table, so a call added without a
signature fails on Linux rather than misbehaving on Windows.
"""


def _kernel32() -> Any:
    """Return ``kernel32`` with signatures applied and ``last_error`` enabled.

    A fresh ``WinDLL`` handle is cheap (the DLL is already loaded and cached by
    ctypes), and applying the signatures every time keeps this the single place
    where a call is configured.
    """
    _require_windows("Win32 access")
    library = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    for name, (restype, argtypes) in SIGNATURES.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = list(argtypes)
    return library


class _FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32))

    def as_posix_timestamp(self) -> float:
        raw = (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)
        return (raw - _FILETIME_EPOCH_DELTA) / _FILETIME_TICKS_PER_SECOND


class _THREADENTRY32(ctypes.Structure):
    """Pointer-width-independent layout from ``tlhelp32.h``."""

    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    )


class _PROCESSENTRY32W(ctypes.Structure):
    """Pointer-width-correct layout from ``tlhelp32.h``."""

    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    )


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
        return _creation_time_from_handle(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _creation_time_from_handle(kernel32: Any, handle: int) -> float | None:
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


def process_is_alive(pid: int) -> bool:
    """True when *pid* names a process that has not yet exited."""
    _require_windows("process_is_alive")
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED means the process exists but belongs to somebody
        # else; that is still "alive", and Devbox will refuse to touch it because
        # the creation time will not match any record it owns.
        return _last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def resume_process_threads(pid: int) -> None:
    """Resume every initial thread of a process created with ``CREATE_SUSPENDED``.

    ``subprocess.Popen`` closes the primary thread handle before returning, so
    Devbox cannot call ``ResumeThread`` on that handle directly.  A Toolhelp
    snapshot is the documented way to recover the thread ID.  Before the
    primary thread is resumed it cannot execute user code or create a child,
    which makes assigning the process to a Job Object race-free.

    The function fails closed: if the process has no discoverable/resumable
    thread, the supervisor terminates it instead of leaving a suspended or
    unsupervised service behind.
    """
    _require_windows("resume_process_threads")
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or int(snapshot) == _INVALID_HANDLE_VALUE:
        raise _win32_error(f"CreateToolhelp32Snapshot failed for pid {pid}")

    resumed = 0
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            raise _win32_error(f"Thread32First failed for pid {pid}")

        while True:
            if int(entry.th32OwnerProcessID) == pid:
                thread_id = int(entry.th32ThreadID)
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_id)
                if not thread:
                    raise _win32_error(f"OpenThread failed for thread {thread_id} of pid {pid}")
                try:
                    previous_count = int(kernel32.ResumeThread(thread))
                    if previous_count == _RESUME_THREAD_FAILED:
                        raise _win32_error(
                            f"ResumeThread failed for thread {thread_id} of pid {pid}"
                        )
                    # CREATE_SUSPENDED adds one suspension.  If another actor
                    # added one before this point, fully resume our brand-new
                    # process rather than silently leaving the service hung.
                    while previous_count > 1:
                        previous_count = int(kernel32.ResumeThread(thread))
                        if previous_count == _RESUME_THREAD_FAILED:
                            raise _win32_error(
                                f"ResumeThread failed for thread {thread_id} of pid {pid}"
                            )
                    resumed += 1
                finally:
                    kernel32.CloseHandle(thread)

            if kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                continue
            error = _last_error()
            if error != ERROR_NO_MORE_FILES:
                raise _win32_error(f"Thread32Next failed for pid {pid}")
            break
    finally:
        kernel32.CloseHandle(snapshot)

    if resumed == 0:
        raise ProcessError(f"No initial thread found for suspended process {pid}")


def _process_parents(kernel32: Any) -> dict[int, int]:
    """Return a point-in-time ``pid -> parent pid`` map from Toolhelp."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == _INVALID_HANDLE_VALUE:
        raise _win32_error("CreateToolhelp32Snapshot failed for process tree")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise _win32_error("Process32FirstW failed for process tree")
        parents: dict[int, int] = {}
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                continue
            error = _last_error()
            if error != ERROR_NO_MORE_FILES:
                raise _win32_error("Process32NextW failed for process tree")
            return parents
    finally:
        kernel32.CloseHandle(snapshot)


def _descendant_pids(parents: dict[int, int], root_pid: int) -> set[int]:
    """Find descendants without following unrelated branches or parent cycles."""
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        found = {
            pid
            for pid, parent_pid in parents.items()
            if pid != root_pid and pid not in descendants and parent_pid in frontier
        }
        if not found:
            break
        descendants.update(found)
        frontier = found
    return descendants


class CapturedProcess:
    """A pinned, verified descendant process handle used during tree shutdown."""

    __slots__ = ("_closed", "_handle", "_kernel32", "creation_time", "pid")

    def __init__(self, handle: int, pid: int, creation_time: float, kernel32: Any) -> None:
        self._handle = handle
        self.pid = pid
        self.creation_time = creation_time
        self._kernel32 = kernel32
        self._closed = False

    def terminate(self, exit_code: int = 1) -> None:
        if self._closed:
            return
        if not self._kernel32.TerminateProcess(self._handle, exit_code):
            last = _last_error()
            code = ctypes.c_uint32(STILL_ACTIVE)
            exited = self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)) and (
                int(code.value) != STILL_ACTIVE
            )
            if not exited and last not in (0, ERROR_INVALID_PARAMETER):
                raise _win32_error(f"TerminateProcess failed for descendant pid {self.pid}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._kernel32.CloseHandle(self._handle)


def capture_descendant_processes(
    root_pid: int,
    root_creation_time: float,
) -> tuple[CapturedProcess, ...]:
    """Pin descendants of a verified root before Job Object termination.

    Two process snapshots surround handle acquisition.  A candidate is retained
    only when its *current* PID still lies below ``root_pid`` in the second
    snapshot and its creation time does not predate the verified root.  The open
    handle pins that exact process object, so later PID reuse cannot redirect a
    termination to an unrelated process.
    """
    _require_windows("capture_descendant_processes")
    kernel32 = _kernel32()
    initial = _descendant_pids(_process_parents(kernel32), root_pid)
    opened: dict[int, CapturedProcess] = {}
    access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE
    try:
        for pid in initial:
            handle = kernel32.OpenProcess(access, False, pid)
            if not handle:
                continue
            created = _creation_time_from_handle(kernel32, handle)
            if created is None or created < root_creation_time:
                kernel32.CloseHandle(handle)
                continue
            opened[pid] = CapturedProcess(int(handle), pid, created, kernel32)

        current = _descendant_pids(_process_parents(kernel32), root_pid)
        verified: list[CapturedProcess] = []
        for pid, process in opened.items():
            if pid in current:
                verified.append(process)
            else:
                process.close()
        return tuple(verified)
    except Exception:
        for process in opened.values():
            process.close()
        raise


class JobObject:
    """An owned Windows Job Object used to supervise one process tree.

    Usage::

        job = JobObject.create("devbox-backend")
        proc = subprocess.Popen(argv, ...)
        job.assign_pid(proc.pid)
        ...
        job.terminate()   # kills the process *and every descendant*
        job.close()

    Devbox creates the root process suspended, assigns it here, then resumes its
    initial thread via :func:`resume_process_threads`.  No user code can run in
    the interval, so descendants cannot escape between creation and assignment.
    """

    __slots__ = ("_closed", "_handle", "_kernel32", "name")

    def __init__(self, handle: int, name: str, kernel32: Any) -> None:
        self._handle = handle
        self.name = name
        self._kernel32 = kernel32
        self._closed = False

    @classmethod
    def create(
        cls,
        name: str,
        *,
        kill_on_close: bool = True,
        object_name: str | None = None,
    ) -> JobObject:
        """Create a job object.

        *object_name* is the Win32 object name.  Devbox passes one — always in
        the ``Local\\`` namespace, which is scoped to the current logon session
        — because ``devbox up`` exits and a later ``devbox down`` has to reopen
        the same job to stop the tree.  Passing ``None`` creates an anonymous job
        reachable only by this process.

        *kill_on_close* is off for detached services (they must outlive
        ``devbox up``) and on in foreground mode, so Ctrl-C tears the tree down.
        """
        _require_windows("JobObject.create")
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, object_name)
        if not handle:
            raise _win32_error(f"CreateJobObjectW failed for job {name!r}")
        job = cls(handle=int(handle), name=name, kernel32=kernel32)
        if kill_on_close:
            job._set_kill_on_close()
        return job

    @classmethod
    def open(cls, object_name: str) -> JobObject | None:
        """Reopen an existing named job, or ``None`` if it no longer exists.

        ``None`` is the normal answer after a reboot, or once every process in
        the job has exited — not an error.
        """
        _require_windows("JobObject.open")
        kernel32 = _kernel32()
        handle = kernel32.OpenJobObjectW(JOB_OBJECT_REOPEN_ACCESS, False, object_name)
        if not handle:
            return None
        return cls(handle=int(handle), name=object_name, kernel32=kernel32)

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
            last = _last_error()
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
    code = _last_error()
    message = ctypes.FormatError(code).strip()  # type: ignore[attr-defined]
    return ProcessError(f"{operation}: [{code}] {message}")
