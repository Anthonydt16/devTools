"""Filesystem operations whose behaviour differs between Windows and POSIX.

Two of them, and both bite in practice:

**Deleting a tree.**  ``shutil.rmtree`` raises ``PermissionError`` on Windows for
a read-only file — and Node, npm and PostgreSQL archives all contain some.  The
POSIX ``unlink`` only needs write permission on the *directory*, so the same call
succeeds there and fails on the target platform.  :func:`remove_tree` clears the
read-only attribute and retries.

**The executable bit.**  It is meaningful on POSIX and meaningless on Windows,
where executability comes from the file extension.  :func:`set_executable` is the
one place that knows this, so nothing else has to branch on the operating system.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from devbox.host.platform_info import is_windows

__all__ = ["executable_bit_is_meaningful", "remove_tree", "set_executable"]


def executable_bit_is_meaningful() -> bool:
    """False on Windows, where the extension decides what can be executed."""
    return not is_windows()


def set_executable(path: Path) -> None:
    """Make *path* executable by everyone who can read it.

    A no-op on Windows.  Without it, a runtime extracted on Linux — the test
    suite, and any CI job that builds an artifact — has a ``node`` that cannot
    be run.
    """
    if not executable_bit_is_meaningful():
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _clear_read_only(_function: Callable[..., object], path: str, _excinfo: object) -> None:
    """``rmtree`` error handler: drop the read-only attribute and let it retry.

    This is the Windows failure mode.  ``shutil.rmtree`` calls ``os.unlink``,
    which fails with ``PermissionError`` when ``FILE_ATTRIBUTE_READONLY`` is set;
    clearing it makes the retry succeed.
    """
    target = Path(path)
    try:
        target.chmod(stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        return


def remove_tree(path: Path, *, best_effort: bool = False) -> bool:
    """Delete a directory tree, coping with read-only files.

    Returns ``True`` when nothing is left.  With *best_effort* a failure is
    reported rather than raised, which is what a cleanup path wants; without it
    the underlying ``OSError`` propagates so a caller that is about to move
    something into this location finds out now instead of failing obscurely
    afterwards.
    """
    if not path.exists():
        return True
    try:
        shutil.rmtree(path, onexc=_clear_read_only)
    except OSError:
        if not best_effort:
            raise
        # A second pass: the handler above may have unlocked what blocked the
        # first one.
        shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def remove_file(path: Path) -> None:
    """Delete a file, clearing the read-only attribute if that is what blocks it."""
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink(missing_ok=True)
        except OSError:
            raise
