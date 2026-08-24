"""Turning a user-written command string into an argv list.

``devbox.yml`` contains ``command: npm run dev`` — a string, because that is what
people write.  Devbox turns it into ``["npm", "run", "dev"]`` and calls
``subprocess.Popen`` with the list.  ``shell=True`` is never used, so:

* a value containing ``&& rm -rf`` is passed as a literal argument, not executed;
* an argument with spaces survives intact;
* no quoting rules differ between PowerShell, ``cmd.exe`` and Devbox.

Splitting uses POSIX rules with quote handling, which matches what a developer
writing a compose-style file expects, on both platforms.  A user who needs exact
control gives a YAML list instead of a string.

On Windows, ``npm``/``yarn``/``pnpm`` are ``.cmd`` shims: ``CreateProcess``
cannot execute them directly, so :func:`resolve_launcher` finds the real file
through the child's own ``PATH`` — never through the machine ``PATH``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from devbox.errors import ConfigError
from devbox.host.platform_info import is_windows

__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "CREATE_SUSPENDED",
    "WINDOWS_EXECUTABLE_EXTENSIONS",
    "ExistsPredicate",
    "creation_flags",
    "environment_for_display",
    "quote_for_display",
    "resolve_executable",
    "split_command",
]


class ExistsPredicate(Protocol):
    """Answers "is this path an executable file?" — injectable so the Windows
    extension rules can be unit tested from Linux."""

    def __call__(self, candidate: str) -> bool: ...


WINDOWS_EXECUTABLE_EXTENSIONS: tuple[str, ...] = (".exe", ".cmd", ".bat", ".com", ".ps1")

# ``shlex`` treats backslashes as escape characters in POSIX mode.  That is
# correct for ordinary command text, but it corrupts an unquoted Windows drive
# path (for example ``C:\\Users\\me\\python.exe``).  Protect only the
# drive-qualified path portions before parsing so shell quoting and the
# documented YAML-list behavior remain unchanged.
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:\\[^\s\"']+)")
_BACKSLASH_PLACEHOLDER = "\x1f"


def _protect_windows_paths(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(1).replace("\\", _BACKSLASH_PLACEHOLDER)

    return _WINDOWS_PATH_RE.sub(replace, text)


def split_command(command: str | Sequence[str], *, where: str = "command") -> tuple[str, ...]:
    """Split a command into argv.

    A sequence is returned as-is (already explicit).  A string is split with
    ``shlex`` in POSIX mode so quotes group arguments and backslashes inside
    double quotes survive — which is what a Windows path in a quoted argument
    needs.
    """
    if not isinstance(command, str):
        argv = tuple(str(part) for part in command)
        if not argv:
            msg = f"{where}: the command is empty"
            raise ConfigError(msg)
        return argv
    text = command.strip()
    if not text:
        msg = f"{where}: the command is empty"
        raise ConfigError(msg)
    try:
        protected = _protect_windows_paths(text)
        parts = [
            part.replace(_BACKSLASH_PLACEHOLDER, "\\")
            for part in shlex.split(protected, posix=True)
        ]
    except ValueError as exc:
        msg = f"{where}: cannot parse {command!r}: {exc}"
        raise ConfigError(
            msg,
            hint="Unbalanced quote? Use a YAML list to pass arguments verbatim.",
        ) from exc
    if not parts:
        msg = f"{where}: the command is empty"
        raise ConfigError(msg)
    return tuple(parts)


def quote_for_display(argv: Sequence[str]) -> str:
    """Render argv for humans.  Display only — never fed back to a shell."""
    return " ".join(shlex.quote(part) for part in argv)


def resolve_executable(
    program: str,
    *,
    path_entries: Sequence[str],
    is_windows: bool,
    exists: ExistsPredicate | None = None,
) -> str | None:
    """Find *program* on the given ``PATH`` entries.

    This is the mechanism that makes two Node versions coexist: the search uses
    the ``PATH`` built for *that child process*, not the machine ``PATH``.  It is
    pure and injectable (*exists*), so the Windows extension rules are unit
    tested on Linux.
    """
    probe = exists if exists is not None else _default_exists

    if any(separator in program for separator in ("/", "\\")):
        return _resolve_candidate(program, is_windows, probe)

    for entry in path_entries:
        if not entry:
            continue
        candidate = f"{entry.rstrip('/\\')}/{program}"
        found = _resolve_candidate(candidate, is_windows, probe)
        if found is not None:
            return found
    return None


def _resolve_candidate(
    candidate: str,
    is_windows: bool,
    probe: ExistsPredicate,
) -> str | None:
    """Resolve one PATH candidate using the target platform's launch rules.

    Fake runtimes intentionally contain extensionless POSIX launchers beside
    Windows ``.exe``/``.cmd`` launchers.  On Windows, the launchable, suffixed
    files must therefore win over an extensionless sibling; returning the
    sibling makes ``CreateProcessW`` fail with ``ERROR_BAD_EXE_FORMAT`` (193).
    An explicitly suffixed candidate is still checked as written.
    """
    if is_windows:
        suffix = Path(candidate).suffix.lower()
        if suffix not in WINDOWS_EXECUTABLE_EXTENSIONS:
            found = _with_extensions(candidate, probe)
            if found is not None:
                return found
    return candidate if probe(candidate) else None


def _with_extensions(
    candidate: str,
    probe: ExistsPredicate,
) -> str | None:
    for extension in WINDOWS_EXECUTABLE_EXTENSIONS:
        with_extension = candidate + extension
        if probe(with_extension):
            return with_extension
    return None


def _default_exists(candidate: str) -> bool:
    path = Path(candidate)
    return path.is_file()


# Win32 process creation flags.  Defined here rather than read from
# ``subprocess`` because the module only exposes them on Windows, and Devbox
# builds the flags on Linux too (they are simply unused there).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_SUSPENDED = 0x00000004


def creation_flags(*, new_process_group: bool = True, own_console: bool = True) -> int:
    """Windows ``creationflags`` for a supervised child.

    The two flags interact, and the interaction is the whole design:

    ``CREATE_NEW_PROCESS_GROUP``
        Always set.  It keeps a Ctrl-C typed in Devbox's own console from
        reaching the services, and it is what makes a group the unit a console
        control event can address.

    ``CREATE_NO_WINDOW`` (*own_console*)
        Set for **detached** services.  The child then has no console shared
        with Devbox, so it survives the terminal window being closed — which is
        what "detached" has to mean.  The cost is that a console control event
        can no longer reach it: ``GenerateConsoleCtrlEvent`` only signals process
        groups attached to the *caller's* console.  Devbox therefore stops such a
        service by terminating its job object, and graceful shutdown where it
        matters — databases — goes through the provider's own stop command
        (``pg_ctl stop -m fast``, ``mysqladmin shutdown``).

        Cleared in **foreground** mode, where the child shares Devbox's console:
        Ctrl-Break then works and is used before terminating, and closing the
        terminal takes everything down, which is what foreground means.

    Zero on POSIX, where the supervisor signals the process group instead.
    """
    if not is_windows():
        return 0
    # Starting suspended closes the only process-tree race in the Job Object
    # design: WindowsSupervisor assigns the root to its job before resuming its
    # initial thread, so no descendant can be created outside the job.
    flags = CREATE_SUSPENDED
    if own_console:
        flags |= CREATE_NO_WINDOW
    if new_process_group:
        flags |= CREATE_NEW_PROCESS_GROUP
    return flags


def environment_for_display(
    env: Mapping[str, str], *, secrets: Sequence[str] = ()
) -> dict[str, str]:
    """Copy of *env* with secret-looking values masked, for logs and ``devbox ps``."""
    masked = {*(name.upper() for name in secrets)}
    out: dict[str, str] = {}
    for key, value in env.items():
        upper = key.upper()
        if upper in masked or any(
            marker in upper for marker in ("PASSWORD", "SECRET", "TOKEN", "APIKEY", "API_KEY")
        ):
            out[key] = "***"
        else:
            out[key] = value
    return out
