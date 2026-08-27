# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

r"""
Exercises the Windows long-path handling against a real filesystem.

The unit tests for `_get_long_path_compatible_path` patch `sys.platform`, so they pin the
*string construction* and nothing else. They cannot tell whether Windows actually accepts
the result -- they would assert a malformed prefix just as happily as a correct one. Every
assertion here is a real file operation that either succeeds or raises OSError.

It is designed to be run twice on the same host, under two different interpreters:

  * a long-path-aware host (stock `python.exe`, which has declared `longPathAware` since
    CPython 3.6), and
  * a host that does NOT declare `longPathAware` (see
    `scripts/make_non_longpathaware_python.py`), standing in for the DCC executables and
    pywin32's `pythonservice.exe` that job attachments code actually runs inside.

On a host with the `LongPathsEnabled` registry setting ON, the second case is the one the
prefix exists for. `--require-host-unaware` asserts we really are in that case before
drawing any conclusion from it, so a silent fallback to the stock interpreter cannot pass
as a successful run.

The harness, the host-capability probe and the first five probes are from
@crowecawcaw's longpath-probe branch. The remaining probes cover the defects found by
review after that branch was written.

Usage:
    python scripted_tests/windows_long_path_probe.py --work-dir C:\japrobe
    python scripted_tests/windows_long_path_probe.py --work-dir C:\japrobe --unc-root \\localhost\jashare
    python scripted_tests/windows_long_path_probe.py --work-dir C:\japrobe --require-host-unaware
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from threading import Lock
from unittest.mock import MagicMock, patch

from s3transfer.utils import OSUtils

import deadline
from deadline.job_attachments._utils import (
    TEMP_DOWNLOAD_ADDED_CHARS_LENGTH,
    WINDOWS_MAX_PATH_LENGTH,
    WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX,
    WINDOWS_UNC_PATH_STRING_PREFIX,
    _get_long_path_compatible_path,
    _is_relative_to,
    _is_windows_long_path_registry_enabled,
    _normalize_windows_path,
)
from deadline.job_attachments.api._utils import _read_manifests
from deadline.job_attachments.api.manifest import _manifest_snapshot
from deadline.job_attachments.asset_manifests import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03 import AssetManifest, ManifestPath
from deadline.job_attachments.download import download_file
from deadline.job_attachments.models import ManifestSnapshot, S3_DATA_FOLDER_NAME
from deadline.job_attachments.upload import S3AssetUploader


class ProbeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


# ---------------------------------------------------------------------------
# Host capability detection
# ---------------------------------------------------------------------------


def is_host_long_path_aware() -> bool:
    r"""
    Whether *this process* can exceed MAX_PATH without the \\?\ prefix.

    Probed behaviourally rather than by reading the manifest, because the effective answer
    is what matters and it depends on both the registry setting and the host executable's
    manifest declaration. Creates a directory tree deep enough to require long-path
    support, then tries to open a file inside it by its plain path.
    """
    import tempfile

    # Not TemporaryDirectory: its cleanup walks plain paths, which is exactly what may not
    # work here, and it raises on failure. Cleaned up below with explicit prefixes.
    tmp = tempfile.mkdtemp()
    try:
        # Build the tree using prefixed paths, so tree creation itself never depends on
        # the capability being measured.
        deep = Path(tmp)
        while len(str(deep)) < WINDOWS_MAX_PATH_LENGTH + 40:
            deep = deep / "segment"
        os.makedirs(WINDOWS_UNC_PATH_STRING_PREFIX + str(deep), exist_ok=True)

        target = deep / "probe.txt"
        with open(WINDOWS_UNC_PATH_STRING_PREFIX + str(target), "w") as fh:
            fh.write("x")

        try:
            with open(str(target)) as fh:
                fh.read()
        except OSError:
            return False
        else:
            return True
    finally:
        shutil.rmtree(WINDOWS_UNC_PATH_STRING_PREFIX + tmp, ignore_errors=True)


def read_machine_registry_setting() -> object:
    r"""
    The machine-wide LongPathsEnabled value, read straight from the registry.

    Read here rather than via `_is_windows_long_path_registry_enabled()`, because that
    helper calls RtlAreLongPathsEnabled, which is PROCESS-scoped despite the name: with
    the registry value at 1 it returns True under stock python.exe and False under an
    otherwise identical copy declaring longPathAware=false. See
    scripted_tests/longpath_api_scope.py.

    The two therefore have to be reported separately, or a run on a machine with the key
    set looks like a run with it unset.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return value
    except FileNotFoundError:
        return "not set"


def report_environment() -> Tuple[object, bool]:
    registry_value = read_machine_registry_setting()
    api_says = _is_windows_long_path_registry_enabled()
    host_aware = is_host_long_path_aware()

    print("--- environment ---")
    print(f"  sys.executable             : {sys.executable}")
    print(f"  sys.version                : {sys.version.splitlines()[0]}")
    print(f"  LongPathsEnabled (registry): {registry_value!r}  (machine-wide, via winreg)")
    print(f"  RtlAreLongPathsEnabled()   : {api_says}  (process-scoped, despite the name)")
    # There is no public API to query a process's own longPathAware manifest flag, so this
    # is measured behaviourally rather than read.
    print(f"  host_long_path_aware       : {host_aware}  (measured behaviourally)")
    print(f"  WINDOWS_MAX_PATH_LENGTH    : {WINDOWS_MAX_PATH_LENGTH}")
    print(f"  TEMP_DOWNLOAD_ADDED_CHARS  : {TEMP_DOWNLOAD_ADDED_CHARS_LENGTH}")
    print()
    return registry_value, host_aware


# ---------------------------------------------------------------------------
# Helpers for building real long paths
# ---------------------------------------------------------------------------


def build_long_dir(root: str, marker: str) -> str:
    """
    Create a real directory tree under `root` whose paths exceed MAX_PATH, and return the
    deepest directory as a plain (unprefixed) string.

    Created with explicit prefixes so setup never depends on the code under test.
    """
    deep = Path(root) / marker
    while len(str(deep)) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH < WINDOWS_MAX_PATH_LENGTH + 30:
        deep = deep / "longsegment"

    os.makedirs(_prefix_for_setup(str(deep)), exist_ok=True)
    return str(deep)


def _prefix_for_setup(path: str) -> str:
    """Apply the correct prefix by hand, independent of the function under test."""
    if path.startswith(WINDOWS_UNC_PATH_STRING_PREFIX):
        return path
    if path.startswith("\\\\"):
        return WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + path[2:]
    return WINDOWS_UNC_PATH_STRING_PREFIX + path


def rmtree_long(path: str) -> None:
    shutil.rmtree(_prefix_for_setup(path), ignore_errors=True)


# ---------------------------------------------------------------------------
# The probes
# ---------------------------------------------------------------------------


def probe_registry_alone_is_insufficient(work_dir: str, host_aware: bool) -> None:
    r"""
    The premise of the PR: on a non-longPathAware host a plain long path fails even with
    the registry setting on, and the \\?\ prefix is what fixes it.

    Skipped when the host *is* long path aware, since there is nothing to demonstrate.
    Also skipped when the machine-wide setting is off: the premise it demonstrates is
    "registry-on is not enough", which is meaningless if the registry itself is off.
    The registry-off configuration is covered by the other probes, which exercise the
    prefix as the sole path to working long-path access.
    """
    if host_aware:
        print("  (skipped: this host is long path aware, so plain long paths work)")
        return

    if read_machine_registry_setting() != 1:
        print(
            "  (skipped: LongPathsEnabled is off, so the registry-on-is-insufficient "
            "premise does not apply here)"
        )
        return

    long_dir = build_long_dir(work_dir, "premise")
    try:
        target = os.path.join(long_dir, "file.txt")

        # Plain path must fail: registry on, host not aware.
        try:
            with open(target, "w") as fh:
                fh.write("x")
        except OSError:
            pass
        else:
            raise ProbeFailure(
                "A plain long path unexpectedly succeeded on a host reporting itself as "
                "not long path aware. The capability probe disagrees with the filesystem."
            )

        # The prefix is what makes it work.
        with open(_prefix_for_setup(target), "w") as fh:
            fh.write("x")

        print(
            "  confirmed: plain long path fails, prefixed long path succeeds, "
            "registry setting notwithstanding"
        )
    finally:
        rmtree_long(long_dir)


def probe_local_long_path(work_dir: str) -> None:
    """A long drive-letter path returned by the helper is accepted by the filesystem."""
    long_dir = build_long_dir(work_dir, "local")
    try:
        target = os.path.join(long_dir, "output.exr")
        check(
            len(target) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH,
            f"Test path is not long enough to exercise the prefix: {len(target)} chars",
        )

        resolved = _get_long_path_compatible_path(target)
        check(
            str(resolved).startswith(WINDOWS_UNC_PATH_STRING_PREFIX),
            f"Expected a prefixed path for a long local path, got {resolved!r}",
        )

        with open(resolved, "w") as fh:
            fh.write("payload")
        check(os.path.isfile(resolved), f"File not found after writing it: {resolved!r}")
        with open(resolved) as fh:
            check(fh.read() == "payload", "Round-tripped content did not match")

        # The temp-download suffix that TEMP_DOWNLOAD_ADDED_CHARS_LENGTH budgets for must
        # also fit, since download writes to that name before renaming.
        temp_name = str(resolved) + ".f307214C"
        with open(temp_name, "w") as fh:
            fh.write("partial")
        os.replace(temp_name, resolved)

        print(f"  wrote, read and renamed a {len(target)}-char path")
    finally:
        rmtree_long(long_dir)


def probe_forward_slashes(work_dir: str) -> None:
    r"""
    Defect 2: the \\?\ prefix disables the normalization that would otherwise accept
    forward slashes, so a prefixed path containing "/" is passed to the filesystem
    verbatim and fails. Callers do supply them, via os.path.join on manifest-derived
    relative paths.
    """
    long_dir = build_long_dir(work_dir, "slashes")
    try:
        target_with_slashes = os.path.join(long_dir, "sub/nested/file.txt").replace("\\", "/")
        os.makedirs(_prefix_for_setup(os.path.join(long_dir, "sub", "nested")), exist_ok=True)

        # Demonstrate the failure mode the fix avoids: naive prefixing keeps the slashes.
        naive = WINDOWS_UNC_PATH_STRING_PREFIX + target_with_slashes
        try:
            with open(naive, "w") as fh:
                fh.write("x")
        except OSError:
            print("  confirmed: prefix + forward slashes is rejected by the filesystem")
        else:
            print(
                "  note: prefix + forward slashes was accepted on this host; the "
                "conversion is still correct, but this host does not demonstrate the bug"
            )
            os.remove(naive)

        # The helper converts separators first, so its result works.
        resolved = _get_long_path_compatible_path(target_with_slashes)
        check(
            "/" not in str(resolved),
            f"Helper left forward slashes in a prefixed path: {resolved!r}",
        )
        with open(resolved, "w") as fh:
            fh.write("x")
        check(os.path.isfile(resolved), f"File not found after writing it: {resolved!r}")

        print("  helper output accepted by the filesystem")
    finally:
        rmtree_long(long_dir)


def probe_unc_long_path(unc_root: str) -> None:
    r"""
    Defect 1: a long network path needs the \\?\UNC\ form. Prefixing \\?\ verbatim
    produces \\?\\\server\share, which Windows rejects -- so before the fix a long path on
    shared storage did not merely stay unprefixed, it became invalid.
    """
    long_dir = build_long_dir(unc_root, "unc")
    try:
        target = os.path.join(long_dir, "scene.aep")

        # Demonstrate that the pre-PR construction is invalid, not just unprefixed.
        malformed = WINDOWS_UNC_PATH_STRING_PREFIX + target
        try:
            with open(malformed, "w") as fh:
                fh.write("x")
        except OSError as exc:
            print(f"  confirmed: the \\\\?\\ + \\\\ form is rejected ({type(exc).__name__})")
        else:
            raise ProbeFailure(
                f"Windows unexpectedly accepted the malformed form {malformed!r}. The "
                "premise of the UNC fix does not hold on this host."
            )

        resolved = _get_long_path_compatible_path(target)
        check(
            str(resolved).startswith(WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX),
            f"Expected the \\\\?\\UNC\\ form for a network path, got {resolved!r}",
        )
        with open(resolved, "w") as fh:
            fh.write("payload")
        check(os.path.isfile(resolved), f"File not found after writing it: {resolved!r}")

        print(f"  wrote a {len(target)}-char UNC path")
    finally:
        rmtree_long(long_dir)


def probe_normalize_round_trip(work_dir: str, unc_root: Optional[str]) -> None:
    """
    Defect 3: `_normalize_windows_path` must invert both prefix forms, because it feeds
    `_is_relative_to` and the session-directory containment check in
    os_file_permission.py. A path that normalizes wrong makes a file legitimately inside
    the session directory compare as outside, raising PathOutsideDirectoryError.

    Verified against real paths on the real filesystem, not string literals.
    """
    roots: List[Tuple[str, str]] = [("local", work_dir)]
    if unc_root:
        roots.append(("unc", unc_root))

    for label, root in roots:
        long_dir = build_long_dir(root, f"normalize-{label}")
        try:
            target = os.path.join(long_dir, "asset.txt")
            with open(_prefix_for_setup(target), "w") as fh:
                fh.write("x")

            prefixed = _get_long_path_compatible_path(target)
            normalized = _normalize_windows_path(prefixed)

            check(
                normalized == Path(target),
                f"[{label}] Normalizing the prefixed form did not restore the original: "
                f"{normalized!r} != {Path(target)!r}",
            )

            # The containment check os_file_permission.py performs. The session directory
            # is the plain root; the file arrives carrying the prefix.
            check(
                _is_relative_to(str(prefixed), root),
                f"[{label}] A file inside {root!r} compared as outside it when carrying "
                f"the long-path prefix. This is the PathOutsideDirectoryError path.",
            )
            # And relative_to, which os_file_permission.py calls directly.
            _normalize_windows_path(prefixed).relative_to(_normalize_windows_path(root))

            print(f"  [{label}] prefix round-trips and containment holds")
        finally:
            rmtree_long(long_dir)


def probe_download_file_writes_to_long_local_path(work_dir: str, host_aware: bool) -> None:
    r"""
    Runs ``download.download_file`` under a non-longPathAware interpreter with a mock
    ``transfer_manager.download`` that models s3transfer's write-``<hex>``-temp-then-
    rename shape via ``s3transfer.utils.OSUtils.get_temp_filename``. Skipped on aware
    hosts, where the negative control below is impossible.

    Follows the ``probe_registry_alone_is_insufficient`` shape (positive + negative
    control) so the probe demonstrates its distinctive capability -- proving the plain
    form is genuinely rejected on this host before claiming credit for the prefixed one
    working. Without the negative control, both assertions would be the same shape as
    ``test_download_file_extended_length_survives_boto3_write_then_rename`` and the
    probe would earn nothing on top of the unit test.

    Complements ``probe_local_long_path``, which exercises the write-then-rename
    mechanism directly against ``_get_long_path_compatible_path``. What this probe adds
    is the surrounding ``download_file`` chain -- destination prefix, ``.parent.mkdir``,
    boto3 handoff -- confirmed against a host that cannot tolerate a plain long path.

    The transfer manager is mocked, so this pins our contract with s3transfer (prefix
    the destination before handoff), not s3transfer's own behaviour. Drift in
    s3transfer's suffix length is pinned separately by the
    ``test_temp_download_added_chars_length_mirrors_s3transfer_suffix`` unit test.
    """
    if host_aware:
        print(
            "  (skipped: this host is long path aware, so the plain form works "
            "regardless of the prefix helper -- there is nothing for the negative "
            "control to demonstrate)"
        )
        return

    long_dir = build_long_dir(work_dir, "download-dest")
    try:
        payload = b"downloaded contents"

        # Negative control: on this (non-longPathAware) host, a plain long temp name
        # -- the exact shape s3transfer would write to under a dropped prefix -- must
        # be rejected by the filesystem. Same idea as `probe_registry_alone_is_insufficient`.
        # Without this, the positive case below succeeds trivially and pins nothing.
        plain_dest = os.path.join(long_dir, "scene.ma")
        plain_temp = plain_dest + ".f307214C"
        try:
            with open(plain_temp, "wb") as fh:
                fh.write(b"x")
        except OSError:
            pass
        else:
            os.remove(_prefix_for_setup(plain_temp))
            raise ProbeFailure(
                "Plain long temp path unexpectedly accepted on a host reporting "
                "itself as non-longPathAware. The capability probe disagrees with "
                "the filesystem, or --require-host-unaware is not doing its job."
            )

        # Positive case: unpatched, download_file prefixes the destination before
        # handoff and the whole write-then-rename chain succeeds where the plain form
        # above just failed.
        received_fileobjs: List[str] = []

        def fake_download(bucket, key, fileobj, subscribers):
            # Temp name from s3transfer's own OSUtils; suffix-length drift is pinned
            # by test_temp_download_added_chars_length_mirrors_s3transfer_suffix.
            received_fileobjs.append(fileobj)
            temp_path = OSUtils().get_temp_filename(fileobj)
            with open(temp_path, "wb") as fh:
                fh.write(payload)
            os.replace(temp_path, fileobj)
            future = MagicMock()
            future.result.return_value = None
            return future

        mock_transfer_manager = MagicMock()
        mock_transfer_manager.download.side_effect = fake_download

        file_path = ManifestPath(
            path="scene.ma", hash="filehash", size=len(payload), mtime=1234000000
        )

        with patch(
            f"{deadline.__package__}.job_attachments.download.get_s3_transfer_manager",
            return_value=mock_transfer_manager,
        ):
            download_file(
                file_path,
                HashAlgorithm.XXH128,
                long_dir,
                Lock(),
                MagicMock(),  # collision_file_dict
                "test-bucket",
                "rootPrefix/Data",
                MagicMock(),  # s3_client (non-None short-circuits get_s3_client)
            )

        check(
            len(received_fileobjs) == 1,
            f"Expected exactly one download call, got {len(received_fileobjs)}",
        )
        fileobj = received_fileobjs[0]
        check(
            fileobj.startswith(WINDOWS_UNC_PATH_STRING_PREFIX),
            f"download_file handed s3transfer a plain long path on a non-longPathAware "
            f"host after the negative control above confirmed that shape is rejected "
            f"here. Got: {fileobj[:80]}",
        )
        final = _get_long_path_compatible_path(os.path.join(long_dir, "scene.ma"))
        check(final.is_file(), f"Expected {final} to exist after download")

        print(
            f"  plain form rejected, download_file prefixed and wrote to a "
            f"{len(long_dir)}-char destination"
        )
    finally:
        rmtree_long(long_dir)


def probe_manifest_snapshot_long_root(work_dir: str) -> None:
    r"""
    Live-path walk: `_manifest_snapshot` -> `_glob._glob_paths(root, ...)` -> `glob.glob`
    on the plain form. `_glob_paths` absolutizes `root` but never applies the \\?\ prefix,
    so on a non-longPathAware host a walk over a root at or past MAX_PATH yields no files.
    `_manifest_snapshot` then returns None (empty manifest -> None), i.e. a task that reports
    success while capturing zero outputs.

    Not a hypothetical: `attachment_upload.py` is the worker's live output-sync entry point,
    and it goes through `_manifest_snapshot` -> `_glob_paths`. AssetSync.sync_outputs (which
    an earlier revision of this PR patched) is deprecated and has no live callers.
    """
    long_dir = build_long_dir(work_dir, "long-root")
    destination = os.path.join(work_dir, "long-root-dest")
    try:
        # File written with the explicit prefix, so setup itself never depends on the
        # capability being measured.
        asset_file = os.path.join(long_dir, "asset.txt")
        with open(_prefix_for_setup(asset_file), "w") as fh:
            fh.write("scene data")

        os.makedirs(destination, exist_ok=True)

        snapshot: Optional[ManifestSnapshot] = _manifest_snapshot(
            root=long_dir, destination=destination, name="probe"
        )
        check(
            snapshot is not None,
            f"_manifest_snapshot returned None for a long root that contains one real "
            f"file. The walk silently found nothing (root length: {len(long_dir)} chars). "
            f"This is the output-sync-reports-success-but-uploads-nothing failure mode.",
        )
        assert snapshot is not None  # for mypy

        manifests = _read_manifests([snapshot.manifest])
        check(
            len(manifests) == 1,
            f"Expected to read back the one manifest just written, got {len(manifests)}",
        )
        recorded = [p.path for p in next(iter(manifests.values())).paths]
        check(
            any(p.endswith("asset.txt") for p in recorded),
            f"Manifest did not list the asset under a {len(long_dir)}-char root; "
            f"paths={recorded!r}. The walk did not see the file.",
        )

        print(f"  captured a file under a {len(long_dir)}-char root")
    finally:
        rmtree_long(long_dir)
        shutil.rmtree(destination, ignore_errors=True)


def probe_manifest_snapshot_long_root_with_diff(work_dir: str) -> None:
    r"""
    Live-path diff branch: `_manifest_snapshot(..., diff=<base_manifest>)` skips the full
    hash pass and enters `_fast_file_list_to_manifest_diff`, which per-file `stat()`s the
    walked results. Without prefixing that stat, the walk succeeds (thanks to the
    `_glob_paths` fix) but the subsequent `stat()` raises WinError 3 on a
    non-longPathAware host -- moving the failure one step later without actually
    resolving it.

    Not a hypothetical: worker-agent's `attachment_upload.py` merges input manifests and
    passes the merged form as `diff=` to `_manifest_snapshot`, so the diff branch is the
    default in the live worker path.
    """
    long_dir = build_long_dir(work_dir, "diff-root")
    destination = os.path.join(work_dir, "diff-root-dest")
    base_dest = os.path.join(work_dir, "diff-root-base")
    try:
        # Deep file, written with an explicit prefix so setup does not depend on the
        # capability being measured.
        asset_file = os.path.join(long_dir, "asset.txt")
        with open(_prefix_for_setup(asset_file), "w") as fh:
            fh.write("scene data")

        os.makedirs(destination, exist_ok=True)
        os.makedirs(base_dest, exist_ok=True)

        # Baseline snapshot (no diff) -- this is what a prior sync would have captured.
        base_snap = _manifest_snapshot(root=long_dir, destination=base_dest, name="base")
        check(base_snap is not None, "base snapshot returned None; walk did not see the file")
        assert base_snap is not None  # for mypy

        # Modify the file so the diff run has to stat it to detect the change.
        with open(_prefix_for_setup(asset_file), "w") as fh:
            fh.write("scene data v2")

        # This is the call that enters _fast_file_list_to_manifest_diff and stats every
        # walked file. Any plain-form stat here fails on a non-longPathAware host.
        snapshot = _manifest_snapshot(
            root=long_dir, destination=destination, name="probe", diff=base_snap.manifest
        )
        check(
            snapshot is not None,
            f"_manifest_snapshot(diff=...) returned None for a modified file under a "
            f"{len(long_dir)}-char root. The fast-diff stat did not see the change.",
        )

        print(f"  fast-diff stat'd a modified file under a {len(long_dir)}-char root")
    finally:
        rmtree_long(long_dir)
        shutil.rmtree(destination, ignore_errors=True)
        shutil.rmtree(base_dest, ignore_errors=True)


def probe_manifest_snapshot_long_destination(work_dir: str) -> None:
    """
    End-to-end through a public-ish entry point: snapshot a root into a destination whose
    path is long. The manifest must actually be written, and the returned path must be the
    plain form, since callers print it and feed it to other tools.
    """
    asset_root = os.path.join(work_dir, "snapshot-root")
    os.makedirs(asset_root, exist_ok=True)
    with open(os.path.join(asset_root, "input.txt"), "w") as fh:
        fh.write("scene data")

    destination = build_long_dir(work_dir, "snapshot-dest")
    try:
        snapshot: Optional[ManifestSnapshot] = _manifest_snapshot(
            root=asset_root, destination=destination, name="probe"
        )
        check(snapshot is not None, "_manifest_snapshot returned None for a non-empty root")
        assert snapshot is not None  # for mypy

        check(
            not snapshot.manifest.startswith(WINDOWS_UNC_PATH_STRING_PREFIX),
            f"Returned manifest path carries the prefix, which callers cannot parse: "
            f"{snapshot.manifest!r}",
        )
        check(
            os.path.isfile(_get_long_path_compatible_path(snapshot.manifest)),
            f"Manifest file was not written: {snapshot.manifest!r}",
        )
        check(
            len(snapshot.manifest) >= WINDOWS_MAX_PATH_LENGTH,
            f"Destination was not actually long ({len(snapshot.manifest)} chars); the "
            "probe did not exercise the prefix",
        )

        print(f"  snapshot wrote a manifest at a {len(snapshot.manifest)}-char path")
        print("  returned path is unprefixed, as callers require")
    finally:
        rmtree_long(destination)
        shutil.rmtree(asset_root, ignore_errors=True)


def probe_manifest_read_back(work_dir: str) -> None:
    """
    Defect 6, read side: the plain-return split above means the read side has to
    re-apply the prefix, or a manifest written to a long destination reads back as
    "not valid" -- an existing file reported as missing.

    Chained onto a real snapshot rather than a hand-written file, so the two halves of
    the split are checked against each other.
    """
    asset_root = os.path.join(work_dir, "readback-root")
    os.makedirs(asset_root, exist_ok=True)
    with open(os.path.join(asset_root, "input.txt"), "w") as fh:
        fh.write("scene data")

    destination = build_long_dir(work_dir, "readback-dest")
    try:
        snapshot = _manifest_snapshot(root=asset_root, destination=destination, name="probe")
        check(snapshot is not None, "_manifest_snapshot returned None for a non-empty root")
        assert snapshot is not None  # for mypy

        manifests = _read_manifests([snapshot.manifest])
        check(
            len(manifests) == 1,
            f"Expected to read back the one manifest just written, got {len(manifests)}",
        )
        print(f"  read back a manifest from a {len(snapshot.manifest)}-char path")
    finally:
        rmtree_long(destination)
        shutil.rmtree(asset_root, ignore_errors=True)


def probe_unresolved_relative_paths(work_dir: str) -> None:
    r"""
    Defect 6/7: paths that arrive from the CLI are not resolved, and `\\?\` requires a
    fully qualified path *and* disables the normalization that would otherwise resolve a
    relative one. So a long relative path was prefixed into a string the filesystem
    rejects -- on the read side an existing manifest reported as "not valid", on the write
    side a snapshot that failed before writing anything.

    Exercised by actually changing directory, since the whole point is that the path is
    interpreted against the cwd.
    """
    root_dir = os.path.join(work_dir, "relative-root")
    os.makedirs(root_dir, exist_ok=True)
    with open(os.path.join(root_dir, "input.txt"), "w") as fh:
        fh.write("scene data")

    # A relative destination long enough that the joined manifest path trips the branch.
    relative_destination = os.path.join("d" * 120, "e" * 120)
    absolute_destination = os.path.join(work_dir, relative_destination)
    os.makedirs(_prefix_for_setup(absolute_destination), exist_ok=True)

    original_cwd = os.getcwd()
    try:
        os.chdir(work_dir)

        # Write side: a relative destination must still be written.
        snapshot = _manifest_snapshot(root=root_dir, destination=relative_destination, name="probe")
        check(snapshot is not None, "_manifest_snapshot returned None for a non-empty root")
        assert snapshot is not None  # for mypy
        check(
            len(snapshot.manifest) >= WINDOWS_MAX_PATH_LENGTH,
            f"The relative path was not long enough to reach the prefix branch "
            f"({len(snapshot.manifest)} chars)",
        )
        check(
            not os.path.isabs(snapshot.manifest),
            f"Returned path should stay relative, as given: {snapshot.manifest!r}",
        )
        check(
            os.path.isfile(_get_long_path_compatible_path(os.path.abspath(snapshot.manifest))),
            f"Manifest was not written for a relative destination: {snapshot.manifest!r}",
        )

        # Read side: the same relative path must read back.
        manifests = _read_manifests([snapshot.manifest])
        check(
            len(manifests) == 1,
            f"Expected to read back the manifest via its relative path, got {len(manifests)}",
        )

        print(f"  wrote and read a {len(snapshot.manifest)}-char relative path")
    finally:
        # Restored before the tree is removed: Windows will not delete a directory that is
        # a process's working directory.
        os.chdir(original_cwd)
        rmtree_long(os.path.join(work_dir, "d" * 120))
        shutil.rmtree(root_dir, ignore_errors=True)


def probe_snapshot_data_dir(work_dir: str) -> None:
    """
    Defect 8: `_snapshot_input_files` creates `snapshot_dir/Data`, the parent of every
    path it then copies into. The copy target was prefixed but this makedirs was not, so
    under a long snapshot dir the directory creation raised before the prefixed copy could
    run.

    Calls the private method directly: `snapshot_assets` above it needs farm and queue IDs
    and a configured S3 bucket, none of which this defect involves.
    """
    source_root = os.path.join(work_dir, "cas-source")
    os.makedirs(source_root, exist_ok=True)
    payload = b"contents"
    with open(os.path.join(source_root, "file.txt"), "wb") as fh:
        fh.write(payload)

    file_hash = "0" * 32
    # Grown rather than built from fixed-length segments, so the threshold is reached
    # regardless of how long --work-dir is. Not created here: the makedirs under test is
    # what has to create it.
    snapshot_root = os.path.join(work_dir, "cas-snapshot")
    snapshot_dir = snapshot_root
    while len(os.path.join(snapshot_dir, S3_DATA_FOLDER_NAME)) <= WINDOWS_MAX_PATH_LENGTH + 20:
        snapshot_dir = os.path.join(snapshot_dir, "longsegment")

    try:
        uploader = S3AssetUploader(s3_max_pool_connections=50, small_file_threshold_multiplier=20)
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="file.txt", hash=file_hash, size=len(payload), mtime=1234567890)
            ],
            total_size=len(payload),
        )

        uploader._snapshot_input_files(
            snapshot_dir=Path(snapshot_dir),
            manifest=manifest,
            source_root=Path(source_root),
        )

        data_dir = _get_long_path_compatible_path(os.path.join(snapshot_dir, S3_DATA_FOLDER_NAME))
        check(data_dir.is_dir(), f"The Data directory was not created: {data_dir!r}")
        # The copy that follows proves the prefixed target was reachable underneath it.
        copied = _get_long_path_compatible_path(
            os.path.join(snapshot_dir, S3_DATA_FOLDER_NAME, f"{file_hash}.xxh128")
        )
        check(copied.is_file(), f"The snapshotted file was not copied into Data: {copied!r}")

        print(f"  created Data and copied into a {len(snapshot_dir)}-char snapshot dir")
    finally:
        rmtree_long(snapshot_root)
        shutil.rmtree(source_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        default="C:\\japrobe",
        help="Local directory to build long paths under. Kept short so paths can grow long.",
    )
    parser.add_argument(
        "--unc-root",
        default=None,
        help=r"A UNC root such as \\localhost\jashare. UNC probes are skipped if omitted.",
    )
    parser.add_argument(
        "--require-host-unaware",
        action="store_true",
        help="Fail unless this interpreter is NOT long path aware. Use for the run under "
        "the patched, non-longPathAware host, so a silent fallback to stock python.exe "
        "cannot pass as a successful run.",
    )
    parser.add_argument(
        "--require-registry-enabled",
        action="store_true",
        help="Fail unless LongPathsEnabled is ON. The registry-on case is the one the "
        "pre-PR code got wrong, so a run meant to cover it must confirm it.",
    )
    parser.add_argument(
        "--require-registry-disabled",
        action="store_true",
        help="Fail unless LongPathsEnabled is OFF. Symmetric to --require-registry-enabled: "
        "a run meant to cover the registry-off configuration must confirm the toggle took "
        "effect and did not silently fall back to the runner default.",
    )
    args = parser.parse_args()

    if args.require_registry_enabled and args.require_registry_disabled:
        parser.error(
            "--require-registry-enabled and --require-registry-disabled are mutually exclusive."
        )

    if sys.platform != "win32":
        print("This probe only does anything on Windows. Nothing to do.")
        return 0

    os.makedirs(args.work_dir, exist_ok=True)

    registry_value, host_aware = report_environment()

    # Checked against the registry rather than RtlAreLongPathsEnabled, which is
    # process-scoped and so returns False on the non-longPathAware leg even with the
    # machine-wide setting on.
    if args.require_registry_enabled and registry_value != 1:
        print(
            f"FAIL: --require-registry-enabled was passed but the machine-wide "
            f"LongPathsEnabled setting is {registry_value!r}, not 1.",
            file=sys.stderr,
        )
        return 2
    if args.require_registry_disabled and registry_value == 1:
        print(
            f"FAIL: --require-registry-disabled was passed but the machine-wide "
            f"LongPathsEnabled setting is {registry_value!r}, not 0 or unset.",
            file=sys.stderr,
        )
        return 2
    if args.require_host_unaware and host_aware:
        print(
            "FAIL: --require-host-unaware was passed but this interpreter IS long path "
            "aware. The non-longPathAware host was not actually used.",
            file=sys.stderr,
        )
        return 2

    probes: List[Tuple[str, Callable[[], None]]] = [
        (
            "premise: registry alone is insufficient",
            lambda: probe_registry_alone_is_insufficient(args.work_dir, host_aware),
        ),
        ("local long path", lambda: probe_local_long_path(args.work_dir)),
        ("forward slashes converted", lambda: probe_forward_slashes(args.work_dir)),
        (
            "normalize round trip and containment",
            lambda: probe_normalize_round_trip(args.work_dir, args.unc_root),
        ),
        (
            "download_file writes to a long local path",
            lambda: probe_download_file_writes_to_long_local_path(args.work_dir, host_aware),
        ),
        (
            "manifest snapshot walks a long root",
            lambda: probe_manifest_snapshot_long_root(args.work_dir),
        ),
        (
            "manifest snapshot diff branch on a long root",
            lambda: probe_manifest_snapshot_long_root_with_diff(args.work_dir),
        ),
        (
            "manifest snapshot to a long destination",
            lambda: probe_manifest_snapshot_long_destination(args.work_dir),
        ),
        ("manifest reads back from a long path", lambda: probe_manifest_read_back(args.work_dir)),
        (
            "unresolved relative paths, read and write",
            lambda: probe_unresolved_relative_paths(args.work_dir),
        ),
        ("snapshot Data directory creation", lambda: probe_snapshot_data_dir(args.work_dir)),
    ]
    if args.unc_root:
        probes.insert(3, ("long UNC path", lambda: probe_unc_long_path(args.unc_root)))
    else:
        print("NOTE: --unc-root not given, skipping the network-path probes.\n")

    failures: List[str] = []
    for name, probe in probes:
        print(f"[ RUN  ] {name}")
        try:
            probe()
        except Exception:
            failures.append(name)
            print(f"[ FAIL ] {name}")
            traceback.print_exc()
        else:
            print(f"[  OK  ] {name}")
        print()

    print("--- summary ---")
    print(f"  {len(probes) - len(failures)} passed, {len(failures)} failed")
    for name in failures:
        print(f"  FAILED: {name}")
    # The file-permission passes in os_file_permission.py are deliberately not probed
    # here: _change_permission_for_windows needs a prepared target user account, which an
    # unattended run does not have. scripted_tests/set_file_permission_for_windows.py
    # covers that path and takes the users as arguments.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
