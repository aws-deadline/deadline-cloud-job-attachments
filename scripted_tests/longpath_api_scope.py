# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

r"""
Determines whether RtlAreLongPathsEnabled() reports the machine-wide registry setting or
the calling process's effective long-path capability.

This distinction is the whole basis for removing the condition

    and not _is_windows_long_path_registry_enabled()

from _get_long_path_compatible_path. If the API is registry-scoped, that condition
skipped the prefix on a non-longPathAware host whenever the key was set -- a bug. If it
is process-scoped, the condition already applied the prefix in exactly that case, and
removing it is hardening rather than a fix. The PR description claims the latter; this
script is how that claim is checked rather than assumed.

Reads the registry directly with winreg and compares it against the API, so the two
sources are independent. Run it under both a stock python.exe and a copy whose manifest
declares longPathAware=false (see scripts/make_non_longpathaware_python.py); the
interesting result is whether the API's answer changes between the two while the registry
value does not.

Adapted from @crowecawcaw's longpath-probe branch.

Usage:
    python scripted_tests/longpath_api_scope.py
"""

from __future__ import annotations

import ctypes
import sys

FILESYSTEM_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"


def read_registry_value() -> object:
    """The machine-wide setting, read directly. Independent of any process state."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, FILESYSTEM_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return value
    except FileNotFoundError:
        return "not set"


def call_rtl_are_long_paths_enabled() -> bool:
    """Exactly what _is_windows_long_path_registry_enabled() calls."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
    ntdll.RtlAreLongPathsEnabled.argtypes = ()
    return bool(ntdll.RtlAreLongPathsEnabled())


def main() -> int:
    if sys.platform != "win32":
        print("This script only does anything on Windows.", file=sys.stderr)
        return 1

    registry_value = read_registry_value()
    api_value = call_rtl_are_long_paths_enabled()

    print(f"interpreter                 : {sys.executable}")
    print(f"HKLM\\...\\LongPathsEnabled   : {registry_value!r}   (read via winreg)")
    print(f"RtlAreLongPathsEnabled()    : {api_value}   (read via ntdll)")

    if registry_value == 1 and not api_value:
        print(
            "\nCONCLUSION: the registry says long paths are enabled machine-wide, but the "
            "API returns False for this process. RtlAreLongPathsEnabled is therefore "
            "PROCESS-SCOPED, not registry-scoped: it folds in the calling process's "
            "longPathAware manifest declaration."
        )
    elif registry_value == 1 and api_value:
        print(
            "\nCONCLUSION for this process: registry on and API True. Consistent with "
            "either scope. Compare against the non-longPathAware run to distinguish."
        )
    else:
        print(
            "\nCONCLUSION: inconclusive here, because the registry setting is not 1. "
            "This comparison needs a machine with LongPathsEnabled=1."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
