# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

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
    _get_long_path_compatible_path,
    _normalize_windows_path,
    _is_relative_to,
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="This test is for paths in POSIX path format and will be skipped on Windows.",
    )
    @pytest.mark.parametrize(
        ("path1", "path2", "expected"),
        [
            ("/a/b/c", "/a/b", True),
            (Path("/a/b/c.txt"), "/a", True),
            ("a/b/c", "a/b", True),
            (Path("a/b/c.txt"), "a", True),
            ("/a/b/c", "a/b", False),
            ("a/b/c", "/a/b", False),
            ("/a/b/c", "/d", False),
            ("a/b/c", "b", False),
            ("a/b/c", "d", False),
        ],
    )
    def test_is_relative_to_on_posix(self, path1, path2, expected):
        """
        Tests if the is_relative_to() works correctly when using Posix paths.
        """
        assert _is_relative_to(path1, path2) == expected

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="This test is for paths in Windows path format and will be skipped on non-Windows.",
    )
    @pytest.mark.parametrize(
        ("path1", "path2", "expected"),
        [
            ("C:/a/b/c", "C:/a/b", True),
            (Path("C:/a/b/c.txt"), "C:/a", True),
            ("C:\\a\\b\\c", "C:\\a\\b", True),
            (Path("C:\\a\\b\\c.txt"), "C:\\a", True),
            ("a/b/c", "a/b", True),
            (Path("a/b/c.txt"), "a", True),
            ("C:/a/b/c", "a/b", False),
            ("a/b/c", "C:/a/b", False),
            ("C:/a/b/c", "C:/d", False),
            ("a/b/c", "b", False),
            ("a/b/c", "d", False),
            (
                "\\\\?\\C:\\path\\to\\a\\very\\long\\file\\path\\that\\exceeds\\the\\windows\\max\\path\\length\\for\\testing\\max\\file\\path\\error\\handling\\when\\comparing\\path\\relativity\\using\\job\\attachments",
                "C:\\path\\to\\",
                True,
            ),
            (
                "\\\\?\\C:\\path\\to\\a\\very\\long\\file\\path\\that\\exceeds\\the\\windows\\max\\path\\length\\for\\testing\\max\\file\\path\\error\\handling\\when\\comparing\\path\\relativity\\using\\job\\attachments",
                "C:\\path\\doesnt\\exist\\",
                False,
            ),
            (
                "\\\\?\\C:\\ProgramData\\Amazon\\OpenJD\\session-612345a668724122b6949a232cb4583e1234567d\\assetroot-777691d8674399c12345\\Desktop\\resources\\isolated-black-tree-silhouettes-white-background-shade-trees-used-product-design-isolated-black-tree-silhouettes-1270.jpg",
                Path(
                    "C:\\ProgramData\\Amazon\\OpenJD\\session-612345a668724122b6949a232cb4583e1234567d\\assetroot-777691d8674399c12345"
                ),
                True,
            ),
        ],
    )
    def test_is_relative_to_on_windows(self, path1, path2, expected):
        """
        Tests if the is_relative_to() works correctly when using Windows paths.
        """
        assert _is_relative_to(path1, path2) == expected

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

    The `\\?\` prefix is what lets a file operation exceed MAX_PATH. Whether it is
    needed depends on the *calling process* declaring longPathAware in its
    application manifest, which is independent of the machine-wide
    LongPathsEnabled registry setting. Deadline code runs inside DCC-hosted
    interpreters whose host executables do not declare the flag, so the prefix is
    required even when the registry setting is on.
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
            "Long paths must get the \\\\?\\ prefix even when the LongPathsEnabled registry "
            "setting is on, because the prefix is what allows a non-longPathAware host process "
            "(e.g. a DCC executable) to exceed MAX_PATH."
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

        assert str(result) == long_posix_path

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
        `_is_relative_to` and the session-directory containment check in
        os_file_permission, so a network path that strips to `UNC\server\share` would
        read as relative and compare unequal against its own normal form.
        """
        assert str(_normalize_windows_path(prefixed)) == expected
