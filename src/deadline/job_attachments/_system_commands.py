# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Resolution of system command names to absolute paths, without consulting PATH.

The VFS mount and unmount paths invoke ``sudo`` to act as the job user, and query
mount state with ``findmnt``. Invoking those by bare name resolves them through
``PATH``; where any part of that search path is influenced by less-trusted input,
the resolution itself is the vulnerability (CWE-426, Untrusted Search Path).

This module removes ``PATH`` from the picture by scanning a fixed list of trusted
absolute directories instead.

Three properties are load-bearing, and each is pinned by a test in
``test/unit/deadline_job_attachments/test_system_commands.py``:

* **``PATH`` is never read.** Not directly, and not indirectly via
  :func:`shutil.which`, which resolves through ``PATH`` and so would reintroduce
  the problem while appearing to fix it.
* **Only paths under :data:`TRUSTED_SYSTEM_DIRECTORIES` are returned**, and a
  name containing a path separator is rejected -- otherwise joining ``/usr/bin``
  with ``../../tmp/evil`` would make this module the injection point it exists to
  remove.
* **A missing command raises.** Falling back to the bare name would restore the
  vulnerability while looking fixed, which is the worst available failure mode
  for this class of fix.

Why a resolver rather than absolute-path literals: the locations are not
universal. NixOS keeps the setuid ``sudo`` wrapper at ``/run/wrappers/bin/sudo``,
so a hardcoded ``/usr/bin/sudo`` would turn a security bug into a mount failure
on those hosts.
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
    # Ordered, deliberately. On NixOS the setuid `sudo` wrapper lives here and the
    # /usr/bin copy is absent or not setuid, so this must be searched first.
    "/run/wrappers/bin",
    "/usr/bin",
    "/bin",
    # sbin last: on non-usr-merged distributions some system commands exist only
    # under /sbin.
    "/usr/sbin",
    "/sbin",
)


class SystemCommandNotFoundError(Exception):
    """A required system command was not present in any trusted directory.

    Deliberately not a subclass of :class:`FileNotFoundError`. Code around
    subprocess invocations catches ``FileNotFoundError`` to mean "this optional
    tool is not installed, carry on degraded", and this condition must not be
    absorbed by that handling: it means a privileged helper is unavailable.
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
    if "/" in name or "\\" in name:
        raise ValueError(
            f"A system command name must not contain a path separator, but got {name!r}."
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
