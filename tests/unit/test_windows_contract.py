"""What can be checked about the Windows code *without* Windows.

The Windows half cannot execute here, but a surprising amount of it is still
verifiable: that every Win32 call has a declared signature, that no module
outside ``host/`` branches on the operating system, that the creation flags are
the pair the design calls for, and that nothing in the package or the test suite
uses a POSIX-only API unguarded.

These are the checks that would have caught the real defects found in review:
a missing ``restype`` truncating a HANDLE, ``os.name`` branching in the archive
extractor, and ``os.killpg`` in a test the Windows CI job runs.
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path

import pytest

import devbox
from devbox.host import winapi
from devbox.host.platform_info import PLATFORM_LINUX_X64, PLATFORM_WINDOWS_X64, is_windows
from devbox.process.windows import job_object_name
from devbox.security.command import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    creation_flags,
)

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(devbox.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]

# POSIX-only names that raise AttributeError on Windows.
POSIX_ONLY = frozenset(
    {"killpg", "getpgid", "setsid", "fork", "getuid", "geteuid", "setuid", "sysconf", "uname"}
)
# Signal names that do not exist on Windows.
POSIX_ONLY_SIGNALS = frozenset({"SIGKILL", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGQUIT", "SIGCHLD"})

# Modules allowed to use them: host/ answers platform questions, and posix.py is
# the POSIX implementation of the supervisor protocol.
PLATFORM_AWARE_MODULES = frozenset(
    {
        "host/procinfo.py",
        "host/platform_info.py",
        "host/winapi.py",
        "host/files.py",
        "process/posix.py",
    }
)


def _module_calls(path: Path) -> set[str]:
    """Attribute names called as ``something.name(...)`` in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _module_attributes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


class TestWin32Signatures:
    """A missing restype truncates a 64-bit HANDLE to 32 bits."""

    def test_every_kernel32_call_has_a_declared_signature(self) -> None:
        called = {
            name for name in _module_calls(PACKAGE_ROOT / "host" / "winapi.py") if name[0].isupper()
        }
        # Structure helpers and ctypes internals are not kernel32 calls.
        called -= {"Structure", "WinDLL", "FormatError"}
        undeclared = called - set(winapi.SIGNATURES)
        assert undeclared == set(), (
            f"kernel32 functions called without a declared signature: {sorted(undeclared)}. "
            "Without argtypes/restype, ctypes assumes a 32-bit int and truncates HANDLEs."
        )

    def test_no_signature_is_declared_for_a_function_never_called(self) -> None:
        called = _module_calls(PACKAGE_ROOT / "host" / "winapi.py")
        unused = set(winapi.SIGNATURES) - called
        assert unused == set(), f"declared but never called: {sorted(unused)}"

    def test_handle_returning_functions_return_a_pointer_not_an_int(self) -> None:
        for name in (
            "OpenProcess",
            "CreateJobObjectW",
            "OpenJobObjectW",
            "CreateToolhelp32Snapshot",
            "OpenThread",
        ):
            restype, _argtypes = winapi.SIGNATURES[name]
            assert restype is ctypes.c_void_p, (
                f"{name} returns a HANDLE; declaring it as {restype} truncates it on Win64"
            )

    def test_handle_taking_functions_accept_a_pointer_not_an_int(self) -> None:
        for name in (
            "CloseHandle",
            "TerminateJobObject",
            "AssignProcessToJobObject",
            "Thread32First",
            "Thread32Next",
            "Process32FirstW",
            "Process32NextW",
            "ResumeThread",
            "TerminateProcess",
        ):
            _restype, argtypes = winapi.SIGNATURES[name]
            assert argtypes[0] is ctypes.c_void_p, f"{name} takes a HANDLE first"

    def test_the_argument_counts_match_the_win32_declarations(self) -> None:
        expected = {
            "OpenProcess": 3,
            "GetProcessTimes": 5,
            "GetExitCodeProcess": 2,
            "CloseHandle": 1,
            "CreateJobObjectW": 2,
            "OpenJobObjectW": 3,
            "SetInformationJobObject": 4,
            "AssignProcessToJobObject": 2,
            "TerminateJobObject": 2,
            "CreateToolhelp32Snapshot": 2,
            "Thread32First": 2,
            "Thread32Next": 2,
            "Process32FirstW": 2,
            "Process32NextW": 2,
            "OpenThread": 3,
            "ResumeThread": 1,
            "TerminateProcess": 2,
        }
        actual = {name: len(argtypes) for name, (_r, argtypes) in winapi.SIGNATURES.items()}
        assert actual == expected

    def test_names_use_the_wide_character_variants(self) -> None:
        """The ANSI variants mangle a path containing an accent."""
        for name in winapi.SIGNATURES:
            if name.endswith("A"):
                pytest.fail(f"{name} is the ANSI variant; use the W variant")

    def test_the_job_object_structures_are_pointer_sized_where_win32_says_so(self) -> None:
        basic = dict(winapi._JOBOBJECT_BASIC_LIMIT_INFORMATION._fields_)
        assert basic["MinimumWorkingSetSize"] is ctypes.c_size_t, "SIZE_T is 64-bit on Win64"
        assert basic["MaximumWorkingSetSize"] is ctypes.c_size_t
        assert basic["Affinity"] is ctypes.c_size_t, "ULONG_PTR is pointer-sized"
        assert basic["LimitFlags"] is ctypes.c_uint32, "DWORD is always 32-bit"

    def test_the_extended_structure_starts_with_the_basic_one(self) -> None:
        fields = winapi._JOBOBJECT_EXTENDED_LIMIT_INFORMATION._fields_
        assert fields[0][0] == "BasicLimitInformation"
        assert fields[0][1] is winapi._JOBOBJECT_BASIC_LIMIT_INFORMATION

    def test_the_filetime_conversion_matches_a_known_value(self) -> None:
        """1601-01-01 + 116444736000000000 ticks is exactly the POSIX epoch."""
        filetime = winapi._FILETIME()
        raw = 116_444_736_000_000_000 + 10_000_000  # epoch + 1 second
        filetime.dwLowDateTime = raw & 0xFFFFFFFF
        filetime.dwHighDateTime = raw >> 32
        assert filetime.as_posix_timestamp() == pytest.approx(1.0)


class TestCreationFlags:
    def test_a_detached_service_gets_its_own_console_and_a_new_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("devbox.security.command.is_windows", lambda: True)
        flags = creation_flags(new_process_group=True, own_console=True)
        assert flags & CREATE_NO_WINDOW
        assert flags & CREATE_NEW_PROCESS_GROUP
        assert flags & CREATE_SUSPENDED

    def test_a_foreground_service_shares_the_console_so_ctrl_break_can_reach_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("devbox.security.command.is_windows", lambda: True)
        flags = creation_flags(new_process_group=True, own_console=False)
        assert not flags & CREATE_NO_WINDOW
        assert flags & CREATE_NEW_PROCESS_GROUP
        assert flags & CREATE_SUSPENDED

    def test_posix_gets_no_creation_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("devbox.security.command.is_windows", lambda: False)
        assert creation_flags() == 0


class TestDescendantSelection:
    def test_only_the_verified_root_branch_is_selected(self) -> None:
        parents = {
            10: 1,
            11: 10,
            12: 11,
            20: 1,
            21: 20,
            30: 30,
        }

        assert winapi._descendant_pids(parents, 10) == {11, 12}

    def test_a_parent_cycle_cannot_pull_in_an_unrelated_branch(self) -> None:
        parents = {10: 1, 11: 10, 40: 41, 41: 40, 42: 41}

        assert winapi._descendant_pids(parents, 10) == {11}


class TestGracefulStopIsHonest:
    """A detached service cannot receive a console control event."""

    def test_a_detached_service_is_not_ctrl_broken(self, tmp_path: Path) -> None:
        from devbox.process.supervisor import LaunchSpec, ProcessRecord
        from devbox.process.windows import WindowsSupervisor

        supervisor = WindowsSupervisor()
        spec = LaunchSpec(
            name="api",
            argv=("node",),
            working_dir=tmp_path,
            env={},
            stdout_path=tmp_path / "o.log",
            stderr_path=tmp_path / "e.log",
            kill_on_supervisor_exit=False,
        )
        supervisor._launch_options(spec)
        record = ProcessRecord(
            project_id="p", service_id="api", pid=1, command=(), working_dir="", started_at=0.0
        )
        assert supervisor._signal_graceful(record) is False, (
            "sending a signal that cannot be delivered would burn the whole "
            "stop_timeout on every devbox down"
        )

    def test_a_foreground_service_is_registered_as_reachable(self, tmp_path: Path) -> None:
        from devbox.process.supervisor import LaunchSpec
        from devbox.process.windows import WindowsSupervisor

        supervisor = WindowsSupervisor()
        spec = LaunchSpec(
            name="api",
            argv=("node",),
            working_dir=tmp_path,
            env={},
            stdout_path=tmp_path / "o.log",
            stderr_path=tmp_path / "e.log",
            kill_on_supervisor_exit=True,
        )
        supervisor._launch_options(spec)
        assert "api" in supervisor._console_attached


class TestJobObjectNaming:
    def test_the_name_is_deterministic(self) -> None:
        assert job_object_name("C:\\p\\app", "backend") == job_object_name("C:\\p\\app", "backend")

    def test_the_name_is_scoped_to_the_logon_session(self) -> None:
        assert job_object_name("p", "s").startswith("Local\\Devbox-")

    def test_the_name_contains_no_backslash_that_would_change_the_namespace(self) -> None:
        name = job_object_name("C:\\projects\\my app", "back\\end")
        assert name.count("\\") == 1

    def test_two_projects_get_two_names(self) -> None:
        assert job_object_name("C:\\a\\app", "svc") != job_object_name("C:\\b\\app", "svc")

    def test_the_name_fits_the_win32_object_name_limit(self) -> None:
        """CreateJobObjectW fails outright on a name longer than MAX_PATH."""
        from devbox.process.windows import MAX_OBJECT_NAME

        name = job_object_name("C:\\" + "x" * 300, "s" * 300)
        assert len(name) <= MAX_OBJECT_NAME

    def test_two_long_service_names_sharing_a_prefix_get_two_jobs(self) -> None:
        first = job_object_name("p", "a" * 60 + "-legacy")
        second = job_object_name("p", "a" * 60 + "-modern")
        assert first != second
        assert len(first) <= 255


class TestNoPlatformAssumptionsLeak:
    def _sources(self, root: Path) -> list[Path]:
        return sorted(root.rglob("*.py"))

    def test_no_module_outside_host_branches_on_the_operating_system(self) -> None:
        offenders: list[str] = []
        for path in self._sources(PACKAGE_ROOT):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            if relative.startswith("host/"):
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {"platform", "name"}:
                    value = node.value
                    if isinstance(value, ast.Name) and value.id in {"sys", "os"}:
                        offenders.append(f"{relative}:{node.lineno} ({value.id}.{node.attr})")
        assert offenders == [], "the operating system may only be inspected in host/: " + ", ".join(
            offenders
        )

    def test_no_posix_only_call_outside_the_platform_aware_modules(self) -> None:
        offenders: list[str] = []
        for path in self._sources(PACKAGE_ROOT):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            if relative in PLATFORM_AWARE_MODULES:
                continue
            used = _module_calls(path) & POSIX_ONLY
            offenders.extend(f"{relative}: {name}" for name in sorted(used))
        assert offenders == [], f"POSIX-only calls that break on Windows: {offenders}"

    def test_no_posix_only_signal_outside_the_platform_aware_modules(self) -> None:
        offenders: list[str] = []
        for path in self._sources(PACKAGE_ROOT):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            if relative in PLATFORM_AWARE_MODULES:
                continue
            used = _module_attributes(path) & POSIX_ONLY_SIGNALS
            offenders.extend(f"{relative}: {name}" for name in sorted(used))
        assert offenders == [], f"POSIX-only signals that do not exist on Windows: {offenders}"

    def test_the_test_suite_uses_no_posix_only_api_outside_conftest(self) -> None:
        """The windows-latest job runs -m integration; a killpg there is an error."""
        tests_root = REPO_ROOT / "tests"
        offenders: list[str] = []
        for path in sorted(tests_root.rglob("*.py")):
            relative = path.relative_to(tests_root).as_posix()
            if relative == "conftest.py":
                continue  # the one place allowed to answer the platform question
            names = (_module_calls(path) & POSIX_ONLY) | (
                _module_attributes(path) & POSIX_ONLY_SIGNALS
            )
            offenders.extend(f"{relative}: {name}" for name in sorted(names))
        assert offenders == [], f"POSIX-only API in a test the Windows job runs: {offenders}"

    def test_no_test_hardcodes_a_posix_child_environment(self) -> None:
        """A Python child on Windows without SystemRoot fails to start."""
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "tests" / "integration").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if '"PATH": "/usr/bin' in source or "'PATH': '/usr/bin" in source:
                offenders.append(path.name)
        assert offenders == [], (
            "these give a child a POSIX-only PATH and no SystemRoot; use "
            f"minimal_child_environment(): {offenders}"
        )


class TestPathSemantics:
    def test_path_separators_differ_and_are_taken_from_the_platform(self) -> None:
        assert PLATFORM_WINDOWS_X64.path_separator == ";"
        assert PLATFORM_LINUX_X64.path_separator == ":"

    def test_the_devbox_home_follows_local_appdata_on_windows(self) -> None:
        from devbox.host.paths import resolve_devbox_home

        home = resolve_devbox_home({"LOCALAPPDATA": "C:\\Users\\dev\\AppData\\Local"})
        assert home.name == "Devbox"
        assert "AppData" in str(home)

    def test_nothing_resolves_into_program_files(self) -> None:
        from devbox.host.paths import resolve_devbox_home

        for environ in (
            {"LOCALAPPDATA": "C:\\Users\\dev\\AppData\\Local"},
            {"USERPROFILE": "C:\\Users\\dev"},
        ):
            assert "Program Files" not in str(resolve_devbox_home(environ))


class TestWinapiIsImportSafe:
    def test_the_module_imports_off_windows(self) -> None:
        assert winapi.JobObject is not None

    def test_every_entry_point_refuses_politely_off_windows(self) -> None:
        from devbox.errors import UnsupportedPlatformError

        if is_windows():  # pragma: no cover - covered by the Windows job
            pytest.skip("this asserts the non-Windows guard")
        with pytest.raises(UnsupportedPlatformError):
            winapi.process_creation_time(1)
        with pytest.raises(UnsupportedPlatformError):
            winapi.process_is_alive(1)
        with pytest.raises(UnsupportedPlatformError):
            winapi.JobObject.create("x")
        with pytest.raises(UnsupportedPlatformError):
            winapi.JobObject.open("x")
