"""Shared fixtures.

Three guarantees this file enforces for every test:

* **Isolation** — ``DEVBOX_HOME`` always points at a temporary directory, so a
  test can never read or write the developer's real ``%LOCALAPPDATA%\\Devbox``.
* **No leaks** — an autouse fixture fails a test that leaves a child process
  behind.  A green suite that leaks ``node`` processes is not green.
* **No network** — nothing here reaches outside the machine.  A "remote"
  registry is an ``http.server`` bound to ``127.0.0.1`` on port 0.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from devbox.host.paths import DEVBOX_HOME_ENV, DevboxPaths, ProjectPaths
from devbox.host.platform_info import PLATFORM_WINDOWS_X64, Platform, current_platform
from devbox.host.procinfo import process_is_alive
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry, build_fake_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip Windows-only tests elsewhere, with a reason the summary can report."""
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(
        reason=f"requires a real Windows host (running on {sys.platform}); "
        "these run on the windows-latest CI job"
    )
    for item in items:
        if "windows" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_devbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point DEVBOX_HOME at a temporary tree for every single test."""
    home = tmp_path / "devbox-home"
    monkeypatch.setenv(DEVBOX_HOME_ENV, str(home))
    yield
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def devbox_home(tmp_path: Path) -> DevboxPaths:
    """A ready-to-use, isolated Devbox home."""
    return DevboxPaths(root=tmp_path / "devbox-home").ensure()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """An empty project directory."""
    path = tmp_path / "project"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def project_with_spaces(tmp_path: Path) -> Path:
    """A project directory whose path contains spaces and a non-ASCII character.

    Windows quoting bugs surface here and nowhere else, so several tests use it
    instead of ``project_dir``.
    """
    path = tmp_path / "My Projects" / "customer portal (café)"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def project_paths(project_dir: Path) -> ProjectPaths:
    return ProjectPaths(project_root=project_dir).ensure()


@pytest.fixture
def write_config(project_dir: Path) -> Callable[[str], Path]:
    """Write ``devbox.yml`` into the project directory and return its path."""

    def _write(text: str, *, name: str = "devbox.yml") -> Path:
        path = project_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    return _write


# ---------------------------------------------------------------------------
# platform helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def host_platform() -> Platform:
    """The platform the tests actually run on."""
    return current_platform()


@pytest.fixture
def windows_platform() -> Platform:
    return PLATFORM_WINDOWS_X64


# ---------------------------------------------------------------------------
# fake runtimes and registries
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_runtime_factory(tmp_path: Path) -> Callable[..., Path]:
    """Build a runnable fake runtime tree on demand."""
    counter = {"n": 0}

    def _build(
        name: str = "node",
        version: str = "0.0.0",
        *,
        extra_executables: Sequence[str] = (),
    ) -> Path:
        counter["n"] += 1
        destination = tmp_path / "runtimes" / f"{name}-{version}-{counter['n']}"
        return build_fake_runtime(
            destination,
            name=name,
            version=version,
            extra_executables=extra_executables,
        )

    return _build


@pytest.fixture
def fake_registry(tmp_path: Path, host_platform: Platform) -> FakeRegistry:
    """A filesystem registry publishing two Node versions for *this* platform.

    Built for the host platform so the install pipeline can actually run the
    result; tests that care about platform mismatches build their own.
    """
    return build_fake_registry(
        tmp_path / "registry",
        [("node", "18.19.0"), ("node", "24.6.0")],
        platform=host_platform.tag,
        extra_executables=("npm",),
    )


@pytest.fixture
def registry_factory(tmp_path: Path) -> Callable[..., FakeRegistry]:
    """Build a registry with arbitrary contents (corrupt digests, other platforms)."""
    counter = {"n": 0}

    def _build(runtimes: Sequence[tuple[str, str]], **kwargs: object) -> FakeRegistry:
        counter["n"] += 1
        root = tmp_path / f"registry-{counter['n']}"
        return build_fake_registry(root, runtimes, **kwargs)  # type: ignore[arg-type]

    return _build


@pytest.fixture
def http_registry_server(fake_registry: FakeRegistry) -> Iterator[str]:
    """Serve the fake registry over loopback HTTP on an ephemeral port.

    This is the only "network" in the suite: a server this process started, on
    127.0.0.1, torn down at the end of the test.
    """
    root = fake_registry.root

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(root), **kwargs)  # type: ignore[arg-type]

        def log_message(self, fmt: str, *args: object) -> None:
            """Silence per-request logging so pytest output stays readable."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# child environments
# ---------------------------------------------------------------------------
def minimal_child_environment(**extra: str) -> dict[str, str]:
    """A minimal but *valid* environment for a Python child on this platform.

    Hardcoding ``PATH=/usr/bin:/bin`` looks harmless and is not: a Python child
    on Windows that inherits no ``SystemRoot`` fails to initialise — sockets and
    the random seed both need it — so the whole integration suite would die on
    the target platform for a reason that has nothing to do with Devbox.
    """
    environment: dict[str, str] = {}
    if sys.platform == "win32":
        for name in ("SystemRoot", "SystemDrive", "TEMP", "TMP", "PATHEXT", "COMSPEC"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        environment.setdefault("PATH", os.environ.get("PATH", ""))
    else:
        environment["PATH"] = "/usr/bin:/bin"
    environment.update(extra)
    return environment


@pytest.fixture
def child_environment() -> Callable[..., dict[str, str]]:
    """Build a child environment that works on this platform."""
    return minimal_child_environment


@pytest.fixture
def hard_kill_outside_devbox() -> Callable[[int, int], None]:
    """Kill a supervised process the way an external `kill -9` or `taskkill` would.

    Used to simulate a service dying behind Devbox's back.  ``os.killpg`` does
    not exist on Windows, so the platform question is answered once, here.
    """

    def _kill(pid: int, process_group: int = 0) -> None:
        if sys.platform == "win32":
            # os.kill with any signal other than CTRL_C/CTRL_BREAK calls
            # TerminateProcess, which is what taskkill /F does.
            os.kill(pid, signal.SIGTERM)
            return
        os.killpg(process_group or pid, signal.SIGKILL)

    return _kill


# ---------------------------------------------------------------------------
# process hygiene
# ---------------------------------------------------------------------------
@pytest.fixture
def spawned_processes() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Register processes here and they are guaranteed to be gone at teardown."""
    processes: list[subprocess.Popen[bytes]] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None:
                stream.close()


@pytest.fixture(autouse=True)
def _no_leaked_children() -> Iterator[None]:
    """Fail a test that leaves a child process of this pytest run behind."""
    before = _child_pids()
    yield
    leaked = _child_pids() - before
    if leaked:
        for pid in leaked:
            _force_kill(pid)
        pytest.fail(f"test leaked child process(es): {sorted(leaked)}")


def _child_pids() -> set[int]:
    """Direct children of this process that are still *running*, on Linux.

    Zombies are excluded on purpose: Devbox starts services detached and does
    not reap them, so a stopped service legitimately lingers as an unreaped
    entry inside a long-lived pytest process.  That is not a leak — the process
    is gone.  A leak is a child still executing after the test finished.

    Empty set elsewhere: on Windows the equivalent needs a toolhelp snapshot,
    and the Windows job asserts on the process tree explicitly instead
    (``tests/windows/test_process_tree.py``).
    """
    children = Path(f"/proc/{os.getpid()}/task")
    if not children.is_dir():
        return set()
    pids: set[int] = set()
    for task in children.iterdir():
        try:
            raw = (task / "children").read_text(encoding="ascii")
        except OSError:
            continue
        pids.update(int(pid) for pid in raw.split())
    return {pid for pid in pids if process_is_alive(pid)}


def _force_kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
@pytest.fixture
def free_port() -> Callable[[], int]:
    """Allocate an ephemeral port.

    Inherently racy in the abstract; in practice the OS does not hand the same
    ephemeral port to two sockets seconds apart, and the alternative (a fixed
    port) is far worse in CI.
    """

    def _allocate() -> int:
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    return _allocate
