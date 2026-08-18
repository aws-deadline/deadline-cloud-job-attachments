# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Resolution of system command names to absolute paths, without consulting PATH.

The problem: the VFS mount and unmount paths invoke ``sudo`` to act as the job
user, and query mount state with ``findmnt``. Invoking those by bare name resolves
them through ``PATH``, which makes the binary actually run depend on the search
path of whatever launched the process, for commands that cross a user boundary.

The solution: callers pass a bare name here and get back an absolute path found by
scanning a fixed list of trusted directories, so ``PATH`` plays no part.

Three properties make that work, and all three are easy to undo by accident:

* ``PATH`` is never read. Not directly, and not through :func:`shutil.which`,
  which resolves via ``PATH`` and so would restore the original behaviour while
  looking like a fix.
* Only paths under :data:`TRUSTED_SYSTEM_DIRECTORIES` are returned. A name
  containing a path separator is rejected, because ``os.path.join`` would
  otherwise let ``../../tmp/evil`` escape the directory being searched.
* A missing command raises. Returning the bare name as a fallback would put
  resolution back on ``PATH`` while the code still read as though it did not.

A resolver rather than absolute-path literals, because the locations are not
universal: NixOS keeps the setuid ``sudo`` wrapper at ``/run/wrappers/bin/sudo``,
so a hardcoded ``/usr/bin/sudo`` would leave those hosts unable to mount at all.
"""

from __future__ import annotations

import os as _os
from typing import Optional as _Optional, Tuple as _Tuple

__all__ = [
    "SystemCommandNotFoundError",
    "TRUSTED_SYSTEM_DIRECTORIES",
    "find_system_command",
    "system_command_path",
]


TRUSTED_SYSTEM_DIRECTORIES: _Tuple[str, ...] = (
    # CodeQL note, referenced by three suppressions in vfs.py.
    #
    # CodeQL's py/clear-text-logging-sensitive-data treats this tuple as a
    # sensitive-data source, so a resolved path such as "/usr/bin/sudo" taints any
    # string it is interpolated into. `build_launch_command` puts that path in the
    # command it returns, and three pre-existing log statements in vfs.py log that
    # command, so those three lines are reported as logging sensitive data in clear
    # text.
    #
    # The finding is a false positive: every value here is a hardcoded absolute
    # system binary directory, and the resolved result is a path to a system
    # executable. Neither is a secret, and none of it is attacker-supplied or
    # user-specific. The log statements are unchanged by the commit that introduced
    # the alerts -- only the dataflow reaching them is new -- so the suppressions
    # record why the flow is benign rather than silencing a genuine leak.
    #
    # If the alternative is preferred, the fix would be to redact or drop the full
    # command from those log lines, which is a change to operator-facing output and
    # so is left to the owners of that logging.
    #
    # Ordered, deliberately. On NixOS the setuid `sudo` wrapper lives here and the
    # /usr/bin copy is absent or not setuid, so this must be searched first.
    "/run/wrappers/bin",
    # ...and these two NixOS entries are a pair. /run/wrappers/bin holds only the
    # setuid/setcap wrappers, so on NixOS it resolves `sudo` and nothing else:
    # /usr/bin holds just `env`, /bin just `sh`, and the sbin directories are
    # absent. `findmnt` lives in this symlink farm, which nixos-rebuild manages and
    # root owns, so it is trust-equivalent to /usr/bin there. Without it the
    # ordering above would resolve `sudo` and then fail on `findmnt`.
    "/run/current-system/sw/bin",
    "/usr/bin",
    "/bin",
    # sbin last: on non-usr-merged distributions some system commands exist only
    # under /sbin.
    "/usr/sbin",
    "/sbin",
)


class SystemCommandNotFoundError(Exception):
    """A required system command was not present in any trusted directory.

    Deliberately not a :class:`FileNotFoundError`, because ``vfs`` already uses that
    type for something else. Three ``except FileNotFoundError`` blocks there mean
    "the VFS pid file is missing", and one of them wraps a call chain that reaches
    this resolver: ``kill_all_processes`` -> ``shutdown_libfuse_mount`` ->
    ``wait_for_mount`` -> ``is_mount``. Inheriting from ``FileNotFoundError`` let a
    resolution failure be reported as a missing pid file and skip the cleanup that
    follows.

    Callers in ``vfs`` translate this into :class:`VFSExecutableMissingError`, which
    is the type their own callers already handle by falling back to a copy-based
    sync. This differs from the sibling resolver in ``openjd-sessions``, where
    inheriting from ``OSError`` is correct because its cancel path deliberately
    catches ``OSError`` so a failed signal cannot unwind a cancelation. Same
    problem, opposite answer, because the surrounding handlers differ.
    """


def _validate_command_name(name: str) -> None:
    """Reject anything that is not a bare command name."""
    if not name:
        raise ValueError("A system command name must not be empty.")
    if name in (_os.curdir, _os.pardir):
        raise ValueError(f"{name!r} is not a system command name.")
    # Both separators are checked on both platforms. A backslash is a legal POSIX
    # filename character, but no command resolved here contains one, and treating
    # it as suspect keeps the check identical rather than subtly weaker on POSIX.
    # The colon is rejected for the same reason, and it is not hypothetical:
    # ntpath.join(r"C:\Windows\System32", "D:evil") == "D:evil". A drive-relative
    # name discards the trusted prefix while containing no separator at all, so a
    # separator-only check lets it through. posixpath joins it harmlessly, but the
    # guard belongs here rather than depending on which os.path is loaded.
    if "/" in name or "\\" in name or ":" in name:
        raise ValueError(
            f"A system command name must not contain a path separator or drive "
            f"specifier, but got {name!r}."
        )


def _is_executable_file(path: str) -> bool:
    return _os.path.isfile(path) and _os.access(path, _os.X_OK)


def find_system_command(name: str) -> _Optional[str]:
    """Return the absolute path to ``name``, or ``None`` if it is not installed.

    ``PATH`` is not consulted. Use this when the command's absence is tolerable;
    use :func:`system_command_path` when it is required.

    Raises:
        ValueError: if ``name`` is not a bare command name.
    """
    _validate_command_name(name)
    for directory in TRUSTED_SYSTEM_DIRECTORIES:
        candidate = _os.path.join(directory, name)
        if _is_executable_file(candidate):
            return candidate
    return None


def system_command_path(name: str) -> str:
    """Return the absolute path to ``name``.

    Raises:
        ValueError: if ``name`` is not a bare command name.
        SystemCommandNotFoundError: if ``name`` is in no trusted directory.
    """
    path = find_system_command(name)
    if path is None:
        raise SystemCommandNotFoundError(
            f"Could not find the system command {name!r} in any trusted directory "
            f"({', '.join(TRUSTED_SYSTEM_DIRECTORIES)}). PATH is deliberately not searched."
        )
    return path
