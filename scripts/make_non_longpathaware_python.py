# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

r"""
Produces a copy of python.exe whose application manifest does NOT declare
longPathAware, and prints its path.

Why this exists: CPython's python.exe has declared longPathAware since 3.6, so a stock
interpreter can exceed MAX_PATH whenever the machine-wide LongPathsEnabled registry
setting is on. That makes a stock interpreter unable to reproduce the failure the \\?\
prefix exists to fix -- any probe run under it passes for the wrong reason.

Job attachments code also runs inside host processes that do NOT declare the flag: DCC
embedded interpreters (Cinema 4D, After Effects) and pywin32's pythonservice.exe. For
those hosts the registry setting has no effect and the prefix is the only mechanism
available. This builds a stand-in for one by copying python.exe and rewriting its
embedded RT_MANIFEST resource with longPathAware set to false. The copy is placed beside
the original so it still resolves sys.prefix and finds its DLLs.

The resource surgery goes through the Win32 resource APIs via ctypes rather than the
SDK's mt.exe: mt.exe refuses to write into some images (it exits 31 on the interpreter in
GitHub's hosted tool cache), and requiring the SDK narrows where this can run.

Approach and the ctypes resource surgery are from @crowecawcaw's longpath-probe branch.

Usage:
    python scripts/make_non_longpathaware_python.py
    python scripts/make_non_longpathaware_python.py --output-name python-nolongpath.exe
"""

from __future__ import annotations

import argparse
import ctypes

# Imported as a submodule rather than `from ctypes import wintypes` so ctypes is not
# brought in two different ways, which CodeQL flags.
import ctypes.wintypes as wintypes
import re
import shutil
import sys
from pathlib import Path

# Resource type for an application manifest, and the id python.exe embeds it under.
RT_MANIFEST = 24
CREATEPROCESS_MANIFEST_RESOURCE_ID = 1
LANG_NEUTRAL = 0

LOAD_LIBRARY_AS_DATAFILE = 0x00000002


class PatchError(RuntimeError):
    pass


def _kernel32() -> ctypes.WinDLL:  # type: ignore[name-defined]
    """kernel32 with the resource APIs given explicit signatures."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    k.LoadLibraryExW.restype = wintypes.HMODULE
    k.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    k.FindResourceW.restype = wintypes.HRSRC
    k.FindResourceW.argtypes = (wintypes.HMODULE, wintypes.LPCWSTR, wintypes.LPCWSTR)
    k.LoadResource.restype = wintypes.HGLOBAL
    k.LoadResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    k.LockResource.restype = wintypes.LPVOID
    k.LockResource.argtypes = (wintypes.HGLOBAL,)
    k.SizeofResource.restype = wintypes.DWORD
    k.SizeofResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    k.FreeLibrary.restype = wintypes.BOOL
    k.FreeLibrary.argtypes = (wintypes.HMODULE,)

    k.BeginUpdateResourceW.restype = wintypes.HANDLE
    k.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    k.UpdateResourceW.restype = wintypes.BOOL
    k.UpdateResourceW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.WORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    k.EndUpdateResourceW.restype = wintypes.BOOL
    k.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    return k


def _int_resource(value: int) -> wintypes.LPCWSTR:
    """
    Wrap an integer id the way MAKEINTRESOURCE does: the low word of the pointer itself,
    rather than a pointer to a string.
    """
    return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)


def read_manifest(exe: Path) -> bytes:
    """The embedded RT_MANIFEST resource bytes."""
    k = _kernel32()
    # AS_DATAFILE: map it for resource reads without running any init code.
    module = k.LoadLibraryExW(str(exe), None, LOAD_LIBRARY_AS_DATAFILE)
    if not module:
        raise PatchError(f"LoadLibraryExW failed for {exe}: {ctypes.get_last_error()}")
    try:
        res = k.FindResourceW(
            module,
            _int_resource(CREATEPROCESS_MANIFEST_RESOURCE_ID),
            _int_resource(RT_MANIFEST),
        )
        if not res:
            raise PatchError(
                f"{exe} has no embedded RT_MANIFEST resource at id "
                f"{CREATEPROCESS_MANIFEST_RESOURCE_ID}. Expected a CPython python.exe."
            )
        size = k.SizeofResource(module, res)
        handle = k.LoadResource(module, res)
        if not handle or not size:
            raise PatchError(f"Failed to load the manifest resource from {exe}")
        pointer = k.LockResource(handle)
        return ctypes.string_at(pointer, size)
    finally:
        k.FreeLibrary(module)


def write_manifest(exe: Path, manifest: bytes) -> None:
    """Replace the embedded RT_MANIFEST resource in place."""
    k = _kernel32()
    # False: keep the other resources (icons, version info) rather than wiping them.
    update = k.BeginUpdateResourceW(str(exe), False)
    if not update:
        raise PatchError(f"BeginUpdateResourceW failed for {exe}: {ctypes.get_last_error()}")

    buffer = ctypes.create_string_buffer(manifest, len(manifest))
    ok = k.UpdateResourceW(
        update,
        _int_resource(RT_MANIFEST),
        _int_resource(CREATEPROCESS_MANIFEST_RESOURCE_ID),
        LANG_NEUTRAL,
        ctypes.cast(buffer, wintypes.LPVOID),
        len(manifest),
    )
    if not ok:
        k.EndUpdateResourceW(update, True)  # discard the pending update
        raise PatchError(f"UpdateResourceW failed for {exe}: {ctypes.get_last_error()}")

    if not k.EndUpdateResourceW(update, False):
        raise PatchError(f"EndUpdateResourceW failed for {exe}: {ctypes.get_last_error()}")


def patch(manifest: bytes) -> bytes:
    """
    Set longPathAware to false.

    Flipped to false rather than deleted: it keeps the manifest schema-valid, and false
    is the documented default for a host that opts out.
    """
    text = manifest.decode("utf-8", errors="strict")
    if "longPathAware" not in text:
        raise PatchError(
            "The source interpreter's manifest does not declare longPathAware, so there "
            "is nothing to turn off. Expected CPython >= 3.6. Aborting rather than "
            "producing a copy that would prove nothing."
        )

    patched, count = re.subn(
        r"(<longPathAware[^>]*>)\s*true\s*(</longPathAware>)",
        r"\1false\2",
        text,
    )
    if count != 1:
        raise PatchError(
            f"Expected exactly one longPathAware=true declaration to rewrite, found "
            f"{count}. The manifest shape may have changed:\n{text}"
        )
    return patched.encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-name",
        default="python-nolongpath.exe",
        help="File name for the patched copy, created beside the source interpreter.",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("This script only does anything on Windows.", file=sys.stderr)
        return 1

    source = Path(sys.executable)
    target = source.parent / args.output_name
    print(f"Source interpreter: {source}")

    shutil.copy2(source, target)

    original = read_manifest(target)
    write_manifest(target, patch(original))

    # Read back, so a silent failure cannot masquerade as a valid non-aware host.
    verify = read_manifest(target).decode("utf-8", errors="replace")
    if not re.search(r"<longPathAware[^>]*>\s*false\s*</longPathAware>", verify):
        raise PatchError(
            f"Read-back check failed: the patched copy still does not declare "
            f"longPathAware=false. Manifest is now:\n{verify}"
        )

    print(f"Patched interpreter is not long path aware: {target}")
    # Last line is the path, so callers can take it with `Select-Object -Last 1`.
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
