"""Host-environment abstractions: filesystem layout, process introspection, Win32 glue.

Everything in this package is allowed to branch on the current operating system.
The rest of Devbox must not: it depends on the protocols exposed here.
"""

from devbox.host.files import (
    executable_bit_is_meaningful,
    remove_file,
    remove_tree,
    set_executable,
)
from devbox.host.paths import DevboxPaths, ProjectPaths, resolve_devbox_home
from devbox.host.platform_info import (
    PLATFORM_WINDOWS_X64,
    Platform,
    current_platform,
    is_linux,
    is_windows,
)
from devbox.host.procinfo import ProcessIdentity, process_creation_time, process_is_alive

__all__ = [
    "PLATFORM_WINDOWS_X64",
    "DevboxPaths",
    "Platform",
    "ProcessIdentity",
    "ProjectPaths",
    "current_platform",
    "executable_bit_is_meaningful",
    "is_linux",
    "is_windows",
    "process_creation_time",
    "process_is_alive",
    "remove_file",
    "remove_tree",
    "resolve_devbox_home",
    "set_executable",
]
