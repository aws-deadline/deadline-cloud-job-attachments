# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import io
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

import deadline
from deadline.job_attachments._utils import (
    TEMP_DOWNLOAD_ADDED_CHARS_LENGTH,
    WINDOWS_MAX_PATH_LENGTH,
    WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX,
    WINDOWS_UNC_PATH_STRING_PREFIX,
    _as_extended_length_path,
    _get_long_path_compatible_path,
    _normalize_windows_path,
    _retry,
)


class TestUtils:
    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="This test is for paths in Windows format and will be skipped on non-Windows systems.",
    )
    @pytest.mark.parametrize(
        ("input_path", "expected"),
        [
            (r"\\?\C:\path\to\file.txt", Path(r"C:\path\to\file.txt")),
            (r"\\?\D:\another\long\path", Path(r"D:\another\long\path")),
            (r"C:\normal\path.txt", Path(r"C:\normal\path.txt")),
            (r"Z:\already\normal\path", Path(r"Z:\already\normal\path")),
            # The \\?\UNC\ form must come back as \\server\share, not UNC\server\share.
            # These paths feed containment checks, so a corrupted form there would make a
            # file inside the session directory look like it sits outside.
            (
                r"\\?\UNC\studio-nas\projects\scene.aep",
                Path(r"\\studio-nas\projects\scene.aep"),
            ),
            (r"\\studio-nas\projects\scene.aep", Path(r"\\studio-nas\projects\scene.aep")),
        ],
    )
    def test_normalize_windows_path(self, input_path, expected):
        """
        Tests if _normalize_windows_path correctly strips the \\?\\ prefix
        from Windows extended-length paths.
        """
        assert _normalize_windows_path(Path(input_path)) == expected

    def test_retry(self):
        """
        Test a function that throws an exception is retried.
        """
        call_count = 0

        # Given
        @_retry(ExceptionToCheck=NotImplementedError, tries=2, delay=0.1, backoff=0.1)
        def test_bad_function():
            nonlocal call_count
            call_count = call_count + 1
            if call_count == 1:
                raise NotImplementedError()

        # When
        test_bad_function()

        # Then
        assert call_count == 2


class TestGetLongPathCompatiblePath:
    r"""
    Tests for _get_long_path_compatible_path.

    The `\\?\` prefix is what lets a file operation exceed MAX_PATH, and it works
    regardless of what the calling process declares.

    The registry-state parametrization below pins that the result does not depend on
    _is_windows_long_path_registry_enabled. Note that helper calls
    RtlAreLongPathsEnabled, which reports the *process's* effective state -- registry
    value AND the executable's longPathAware manifest declaration -- not the registry
    value alone, so it returns False in a non-aware host even with the key set. The
    prefix is now applied on length alone, which is deterministic either way.
    """

    _REGISTRY_CHECK = (
        f"{deadline.__package__}.job_attachments._utils._is_windows_long_path_registry_enabled"
    )

    def _long_path(self) -> str:
        """A Windows path long enough to require the prefix, accounting for temp-download suffix."""
        needed = WINDOWS_MAX_PATH_LENGTH - TEMP_DOWNLOAD_ADDED_CHARS_LENGTH
        path = "C:\\" + "a" * needed
        assert len(path) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH
        return path

    @pytest.mark.parametrize("registry_enabled", [True, False])
    def test_long_path_always_gets_unc_prefix_on_windows(self, registry_enabled):
        """A long path gets the prefix regardless of the registry setting."""
        long_path = self._long_path()

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=registry_enabled
        ):
            result = _get_long_path_compatible_path(long_path)

        assert str(result).startswith(WINDOWS_UNC_PATH_STRING_PREFIX), (
            "Long paths must get the \\\\?\\ prefix regardless of what "
            "_is_windows_long_path_registry_enabled reports, so that the behaviour is "
            "determined by path length alone."
        )
        assert str(result) == WINDOWS_UNC_PATH_STRING_PREFIX + long_path

    @pytest.mark.parametrize("registry_enabled", [True, False])
    def test_is_idempotent(self, registry_enabled):
        """An already-prefixed path is not prefixed twice."""
        already_prefixed = WINDOWS_UNC_PATH_STRING_PREFIX + self._long_path()

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=registry_enabled
        ):
            result = _get_long_path_compatible_path(already_prefixed)

        assert str(result) == already_prefixed

    def test_short_path_is_unchanged_on_windows(self):
        """Paths under the limit are left alone."""
        short_path = r"C:\short\path.txt"

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            result = _get_long_path_compatible_path(short_path)

        assert str(result) == short_path

    def test_non_windows_is_unchanged(self):
        """The prefix is Windows-only and must never be applied elsewhere."""
        long_posix_path = "/" + "a" * WINDOWS_MAX_PATH_LENGTH

        with patch.object(sys, "platform", "linux"):
            result = _get_long_path_compatible_path(long_posix_path)

        # Compared as Path, not str: patching sys.platform does not change pathlib's
        # flavour, so on a Windows runner str(Path("/a")) is "\a" and a string compare
        # against the input would fail for a reason unrelated to the prefix logic.
        assert result == Path(long_posix_path)
        assert WINDOWS_UNC_PATH_STRING_PREFIX not in str(result)

    def test_single_dot_segments_are_normalized_away(self):
        r"""
        "." segments must not survive into a prefixed path.

        The \\?\ prefix turns off the Win32 normalization that would otherwise drop them,
        so a survivor is passed to the filesystem literally. Unlike "..", collapsing "."
        cannot change which file is targeted, so it is safe to do lexically.
        """
        padding = "a" * WINDOWS_MAX_PATH_LENGTH
        path = f"C:\\assets\\project\\.\\shared\\{padding}\\scene.ma"
        assert len(path) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            result = _get_long_path_compatible_path(path)

        assert str(result).startswith(WINDOWS_UNC_PATH_STRING_PREFIX)
        body = str(result)[len(WINDOWS_UNC_PATH_STRING_PREFIX) :]
        assert "\\.\\" not in body, f"the dot segment survived: {body[:60]}"

    @pytest.mark.parametrize(
        ("path_template", "description"),
        [
            pytest.param(
                "C:\\assets\\project\\..\\shared\\{padding}\\scene.ma", "drive", id="drive"
            ),
            pytest.param(
                "\\\\studio-nas\\projects\\a\\..\\b\\{padding}\\scene.aep", "network", id="network"
            ),
        ],
    )
    def test_dotdot_is_rejected_rather_than_collapsed(self, path_template, description):
        r"""
        A ".." in a long path must raise, not be silently collapsed.

        Collapsing lexically would be unsound. Callers validate first with symlink-aware
        resolution and only then call this function -- download_file runs
        _ensure_paths_within_directory (which uses Path.resolve()) at download.py:514
        before prefixing at :517. If a component is a symlink the two disagree, so a
        lexical collapse can write somewhere other than the location that was validated:

            root C:\dl, C:\dl\link -> C:\dl\sub\deep, manifest path link\..\..\evil.txt
            validated (resolve):  C:\dl\evil.txt   -- inside the root, accepted
            written   (lexical):  C:\evil.txt      -- outside the root

        A literal ".." past the prefix was already rejected by the filesystem, so raising
        here loses nothing: it turns an opaque OSError into an explicit error, rather than
        turning a loud failure into a silent escape.
        """
        path = path_template.format(padding="a" * WINDOWS_MAX_PATH_LENGTH)

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            with pytest.raises(ValueError, match=r"\.\."):
                _get_long_path_compatible_path(path)

    def test_dotdot_is_allowed_in_short_paths(self):
        r"""
        The rejection is scoped to the branch that applies the prefix.

        Short paths are returned untouched, so Win32 still resolves their ".." normally.
        Raising for them would break callers that legitimately pass unresolved paths.
        """
        path = "C:\\assets\\project\\..\\shared\\scene.ma"

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            assert _get_long_path_compatible_path(path) == Path(path)

    def test_relative_long_path_falls_through_to_plain(self):
        r"""
        Relative long paths must not raise: callers such as _FileStatCache._get_stat
        and os_file_permission._change_permissions_for_windows catch only
        (FileNotFoundError, PermissionError, OSError) from the subsequent filesystem
        call and rely on the helper never raising. Turning that into a ValueError
        would propagate uncaught. ".." rejection is intentional and kept; only the
        "relative" branch of _as_extended_length_path is softened here.
        """
        path = "relative\\project\\" + ("a" * WINDOWS_MAX_PATH_LENGTH) + "\\scene.ma"

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            assert _get_long_path_compatible_path(path) == Path(path)

    def test_network_path_keeps_leading_pair_through_normalization(self):
        r"""
        Normalizing must not eat the leading pair of backslashes on \\server\share.

        A network path that lost one would stop being a network path, and the \\?\UNC\
        branch would not be taken.
        """
        padding = "a" * WINDOWS_MAX_PATH_LENGTH
        path = f"\\\\studio-nas\\projects\\.\\b\\{padding}\\scene.aep"

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            result = _get_long_path_compatible_path(path)

        expected = (
            WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + f"studio-nas\\projects\\b\\{padding}\\scene.aep"
        )
        assert str(result) == expected

    def _long_network_path(self) -> str:
        r"""A long \\server\share path, the shape studio shared storage uses."""
        path = "\\\\studio-nas\\projects\\" + "a" * WINDOWS_MAX_PATH_LENGTH + "\\scene.aep"
        assert len(path) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH
        return path

    @pytest.mark.parametrize("registry_enabled", [True, False])
    def test_long_network_path_gets_unc_device_prefix(self, registry_enabled):
        r"""
        A \\server\share path needs the \\?\UNC\ form, with the leading pair of
        backslashes replaced. Prepending \\?\ verbatim yields \\?\\\server\share,
        which Windows rejects, so a long path on shared storage would fail to open.
        """
        network_path = self._long_network_path()

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=registry_enabled
        ):
            result = _get_long_path_compatible_path(network_path)

        assert str(result) == WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + network_path[2:]
        assert not str(result).startswith(WINDOWS_UNC_PATH_STRING_PREFIX + "\\")

    @pytest.mark.parametrize("registry_enabled", [True, False])
    def test_network_path_is_idempotent(self, registry_enabled):
        r"""An already \\?\UNC\ prefixed path is not prefixed twice."""
        already_prefixed = WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + self._long_network_path()[2:]

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=registry_enabled
        ):
            result = _get_long_path_compatible_path(already_prefixed)

        assert str(result) == already_prefixed

    def test_short_network_path_is_unchanged(self):
        r"""Network paths under the limit are left alone."""
        short_network_path = r"\\studio-nas\projects\scene.aep"

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=False
        ):
            result = _get_long_path_compatible_path(short_network_path)

        assert str(result) == short_network_path

    @pytest.mark.parametrize("registry_enabled", [True, False])
    def test_forward_slashes_are_normalized_before_prefixing(self, registry_enabled):
        r"""
        The \\?\ prefix turns off the Win32 path normalization that would otherwise
        accept forward slashes, so separators must be converted first. Callers do
        pass forward-slash strings; os.path.join output on a manifest-derived path
        is one such source.
        """
        long_forward_slash_path = "C:/" + "a" * (
            WINDOWS_MAX_PATH_LENGTH - TEMP_DOWNLOAD_ADDED_CHARS_LENGTH
        )

        with patch.object(sys, "platform", "win32"), patch(
            self._REGISTRY_CHECK, return_value=registry_enabled
        ):
            result = _get_long_path_compatible_path(long_forward_slash_path)

        assert "/" not in str(result), (
            "A \\\\?\\ path is passed to the filesystem verbatim, so forward slashes "
            "must be converted to backslashes before the prefix is applied."
        )
        assert str(result) == WINDOWS_UNC_PATH_STRING_PREFIX + long_forward_slash_path.replace(
            "/", "\\"
        )

    @pytest.mark.parametrize(
        "prefixed, expected",
        [
            (
                WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + r"studio-nas\projects\scene.aep",
                r"\\studio-nas\projects\scene.aep",
            ),
            (WINDOWS_UNC_PATH_STRING_PREFIX + r"C:\projects\scene.aep", r"C:\projects\scene.aep"),
        ],
        ids=["network", "drive-letter"],
    )
    def test_prefix_is_stripped_back_to_the_original_form(self, prefixed, expected):
        r"""
        Stripping has to invert prefixing for both forms. `_normalize_windows_path` feeds
        `Path.is_relative_to` and the session-directory containment check in
        os_file_permission, so a network path that strips to `UNC\server\share` would
        read as relative and compare unequal against its own normal form.
        """
        assert str(_normalize_windows_path(prefixed)) == expected


class TestAsExtendedLengthPath:
    r"""
    Tests for _as_extended_length_path, the unconditional variant used for directory
    walks. A walk ROOT can be well under MAX_PATH while paths yielded beneath it exceed
    it, so `asset_sync._get_output_files` prefixes the root regardless of its length and
    lets every yielded path inherit the prefix. Without it, in a process that is not
    longPathAware, `stat()` on a yielded >260-char path fails with WinError 3 even when
    the LongPathsEnabled registry value is set (the customer configuration in
    deadline-cloud-worker-agent#520).
    """

    @pytest.mark.parametrize(
        ("original", "expected"),
        [
            (r"C:\short\path", WINDOWS_UNC_PATH_STRING_PREFIX + r"C:\short\path"),
            (
                r"\\studio-nas\projects\assets",
                WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + r"studio-nas\projects\assets",
            ),
            ("C:/mixed/separators", WINDOWS_UNC_PATH_STRING_PREFIX + r"C:\mixed\separators"),
        ],
        ids=["short-drive-letter", "network", "forward-slashes"],
    )
    def test_prefixes_regardless_of_length(self, original, expected):
        """Short paths are prefixed too: the length gate belongs to the caller's
        context (single file op) and is deliberately absent here (walk root)."""
        with patch.object(sys, "platform", "win32"):
            assert str(_as_extended_length_path(original)) == expected

    def test_already_prefixed_is_unchanged(self):
        prefixed = WINDOWS_UNC_PATH_STRING_PREFIX + r"C:\already\prefixed"
        with patch.object(sys, "platform", "win32"):
            assert str(_as_extended_length_path(prefixed)) == prefixed

    @pytest.mark.parametrize("relative", [r"relative\path", r"C:relative\path"])
    def test_relative_path_is_rejected(self, relative):
        """The extended-length form is only defined for absolute paths."""
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(ValueError, match="absolute path"):
                _as_extended_length_path(relative)

    def test_single_dot_segments_are_normalized(self):
        with patch.object(sys, "platform", "win32"):
            result = _as_extended_length_path(r"C:\projects\.\assets")

        assert str(result) == WINDOWS_UNC_PATH_STRING_PREFIX + r"C:\projects\assets"

    def test_dotdot_is_rejected(self):
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(ValueError, match=r"\.\."):
                _as_extended_length_path(r"C:\projects\..\assets")

    def test_non_windows_is_unchanged(self):
        with patch.object(sys, "platform", "linux"):
            assert _as_extended_length_path("/aaa/bbb") == Path("/aaa/bbb")

    def test_round_trips_through_normalize(self):
        """_normalize_windows_path must invert this helper for both path shapes, since
        the walk keeps plain forms for bookkeeping and prefixed forms for file ops."""
        for original in (r"C:\projects\scene.aep", r"\\studio-nas\projects\scene.aep"):
            with patch.object(sys, "platform", "win32"):
                prefixed = _as_extended_length_path(original)
            assert str(_normalize_windows_path(prefixed)) == original


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="The long path prefix only means anything to the Windows filesystem.",
)
class TestGetLongPathCompatiblePathAgainstRealFilesystem:
    r"""
    Hands the rewritten path to Windows instead of only comparing strings.

    The tests above patch `sys.platform`, so they pin the prefix *string construction* and
    never hand the result to the filesystem -- they would assert a malformed prefix just as
    happily as a correct one. These run unpatched on the Windows CI jobs, where
    `sys.platform` is genuinely win32, and open a real file at a real path over MAX_PATH.
    So a form Windows rejects fails here.
    """

    def _make_long_dir(self, root: Path) -> Path:
        r"""
        Creates a real directory under `root` whose path exceeds MAX_PATH.

        The directories are created through the \\?\ prefix, because creating them is
        itself a filesystem operation subject to MAX_PATH.
        """
        long_dir = root
        while len(str(long_dir)) < WINDOWS_MAX_PATH_LENGTH:
            long_dir = long_dir / ("a" * 10)
        os.makedirs(WINDOWS_UNC_PATH_STRING_PREFIX + str(long_dir), exist_ok=True)
        return long_dir

    def _write_long_file(self, tmp_path: Path, contents: bytes) -> Path:
        """Writes a file at a path over MAX_PATH and returns its unprefixed path."""
        long_file = self._make_long_dir(tmp_path) / "scene.ma"
        with open(WINDOWS_UNC_PATH_STRING_PREFIX + str(long_file), "wb") as f:
            f.write(contents)
        return long_file

    def _read_through(self, path: Path) -> bytes:
        """
        Opens `path` via the helper and returns its contents.

        Uses os.open directly rather than the helpers in upload.py, which swallow OSError
        into a None yield and would report a prefix Windows rejected as an unexplained None.
        """
        fd = os.open(str(_get_long_path_compatible_path(path)), os.O_RDONLY)
        try:
            return os.read(fd, io.DEFAULT_BUFFER_SIZE)
        finally:
            os.close(fd)

    @pytest.mark.parametrize(
        "separator",
        [
            pytest.param("\\", id="backslash"),
            pytest.param("/", id="forward_slash"),
        ],
    )
    def test_long_path_opens(self, tmp_path: Path, separator: str):
        r"""
        A path over MAX_PATH opens after the helper rewrites it.

        The forward-slash case is a distinct defect: the \\?\ prefix turns off the Win32
        normalization that otherwise accepts `/`, so a prefixed path containing forward
        slashes goes to the filesystem verbatim and fails. Callers do supply them --
        `os.path.join` output on manifest-derived relative paths is one source.

        Note this does not by itself distinguish the fix from a host that merely has
        LongPathsEnabled set, since the `python.exe` running these tests is manifest-aware
        and honours that setting. It pins that the string we build is one Windows accepts.
        See test_long_network_path_opens for the case that does isolate the fix.
        """
        contents = b"long path input file"
        long_file = self._write_long_file(tmp_path, contents)

        requested = Path(str(long_file).replace("\\", separator))
        assert len(str(requested)) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH > WINDOWS_MAX_PATH_LENGTH

        assert self._read_through(requested) == contents

    def test_long_path_with_single_dot_segments_opens(self, tmp_path: Path):
        r"""
        A long path containing `.` opens after the helper normalizes it.

        This is the case that motivated normalizing at all: once \\?\ is applied, Windows
        stops resolving dot segments and rejects the path. A string-level test cannot show
        that rejection -- only a real open can.

        Built as a string because Path() drops `.` at construction, which would make the
        test pass without the helper doing anything.
        """
        contents = b"dot segment long path"
        long_file = self._write_long_file(tmp_path, contents)

        # <dir>\.\scene.ma
        with_dot = f"{long_file.parent}\\.\\{long_file.name}"
        assert len(with_dot) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH > WINDOWS_MAX_PATH_LENGTH

        assert self._read_through(Path(with_dot)) == contents

    def test_long_path_with_dotdot_is_refused_before_reaching_the_filesystem(self, tmp_path: Path):
        r"""
        `..` in a long path raises rather than being collapsed -- see
        test_dotdot_is_rejected_rather_than_collapsed for why.

        Kept here as well as at the string level to pin that the pre-existing behaviour is
        not regressed: Windows already rejected a literal `..` past the prefix, so this
        path never opened. The change swaps an opaque OSError for an explicit ValueError.
        """
        long_file = self._write_long_file(tmp_path, b"dot segment long path")

        # Detour through the parent and back: <dir>\<name>\..\<name>\scene.ma
        leaf = long_file.parent
        with_dotdot = leaf / ".." / leaf.name / long_file.name
        assert ".." in str(with_dotdot)
        assert len(str(with_dotdot)) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH > WINDOWS_MAX_PATH_LENGTH

        with pytest.raises(ValueError, match=r"\.\."):
            self._read_through(with_dotdot)

    def test_long_network_path_opens(self, tmp_path: Path):
        r"""
        A long path on a network share opens, which requires the \\?\UNC\ form.

        Prepending \\?\ verbatim to \\server\share yields \\?\\\server\share, which
        Windows rejects outright -- so a long path on shared storage did not merely stay
        long, it became malformed. Studio asset roots are commonly UNC rather than a mapped
        drive, so this is the shape that matters for shared storage.

        Unlike the drive-letter case this one isolates the fix: LongPathsEnabled cannot
        rescue a malformed prefix, so it fails on any host lacking the \\?\UNC\ conversion.

        Uses the built-in C$ administrative share to get a genuine UNC path without `net
        share` setup. That share requires Administrator and a running Server service, so
        the test skips when it is unreachable rather than reporting an environment property
        as a defect.
        """
        contents = b"long unc path input file"
        long_file = self._write_long_file(tmp_path, contents)

        drive, drive_relative = os.path.splitdrive(str(long_file))
        share_root = f"\\\\localhost\\{drive[0]}$"
        try:
            os.stat(share_root + "\\")
        except OSError as e:
            pytest.skip(f"The {share_root} administrative share is not reachable: {e}")

        unc_path = Path(share_root + drive_relative)
        assert len(str(unc_path)) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH > WINDOWS_MAX_PATH_LENGTH

        assert str(_get_long_path_compatible_path(unc_path)).startswith(
            WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX
        ), "A network path must take the \\\\?\\UNC\\ form"
        assert self._read_through(unc_path) == contents
