# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import json
import shutil
from typing import Dict
import pytest
from unittest.mock import patch

from deadline.job_attachments._utils import (
    TEMP_DOWNLOAD_ADDED_CHARS_LENGTH,
    WINDOWS_MAX_PATH_LENGTH,
    _get_long_path_compatible_path,
)
from deadline.job_attachments.exceptions import NonValidInputError
from deadline.job_attachments.asset_manifests.base_manifest import BaseAssetManifest
from deadline.job_attachments.api._utils import _read_manifests


class TestReadManifests:
    def test_valid_manifests(self, temp_dir, test_manifest_one):
        """Test valid manifest file for read

        Args:
            temp_dir: a temporary directory
            test_manifest_one: test manifest
        """

        # Given
        manifest_file_name = "manifest_1"
        file_path = os.path.join(temp_dir, manifest_file_name)

        with open(file_path, "w", encoding="utf8") as f:
            json.dump(test_manifest_one, f)

        # When
        result: Dict[str, BaseAssetManifest] = _read_manifests([file_path])

        # Then
        assert len(result) == 1
        assert result.get(manifest_file_name) is not None

        manifest = result.get(manifest_file_name)
        assert isinstance(manifest, BaseAssetManifest)
        assert len(manifest.paths) == 3

    def test_invalid_file_path(self):
        """
        Test with non-existent file
        """

        with patch("os.path.isfile", return_value=False):
            with pytest.raises(NonValidInputError) as exc_info:
                _read_manifests(["/path/to/nonexistent.json"])

            assert "not valid" in str(exc_info.value)

    def test_empty_manifest_list(self):
        """
        Test with empty input
        """

        # When
        result = _read_manifests([])

        # Then
        assert isinstance(result, dict)
        assert len(result) == 0

    def _write_long_manifest(self, temp_dir, contents, name) -> str:
        """
        Writes a manifest whose relative path alone exceeds MAX_PATH, and returns that
        relative path. The cwd is the caller's responsibility.

        Created through _get_long_path_compatible_path because the absolute form is over
        the limit too, so plain makedirs/open cannot reach it on Windows.
        """
        nested = os.path.join(temp_dir, "d" * 120, "e" * 120)
        os.makedirs(_get_long_path_compatible_path(nested), exist_ok=True)
        with open(
            _get_long_path_compatible_path(os.path.join(nested, name)), "w", encoding="utf8"
        ) as f:
            json.dump(contents, f)
        return os.path.join("d" * 120, "e" * 120, name)

    def _remove_long_tree(self, temp_dir) -> None:
        """
        Drops the long tree before the temp_dir fixture unwinds.

        TemporaryDirectory tears down with an unprefixed shutil.rmtree, which cannot remove
        a >MAX_PATH tree on Windows and would error at teardown.
        """
        shutil.rmtree(
            _get_long_path_compatible_path(os.path.join(temp_dir, "d" * 120)), ignore_errors=True
        )

    def test_long_relative_path_is_read(self, temp_dir, test_manifest_one, monkeypatch):
        r"""
        A relative path long enough to trip the prefix branch is still read.

        Unlike every other caller of _get_long_path_compatible_path, the paths here come
        from the CLI unresolved. `\\?\` requires a fully qualified path *and* turns off the
        normalization that would otherwise resolve a relative one, so prefixing a long
        relative path yields a string the filesystem rejects -- `os.path.isfile` returns
        False and an existing manifest is reported as "not valid". Made absolute before
        prefixing to avoid that.

        Runs on all platforms: the prefix branch is Windows-only, so elsewhere this just
        pins that a long relative path keeps working.
        """
        manifest_file_name = "manifest_rel"
        try:
            relative = self._write_long_manifest(temp_dir, test_manifest_one, manifest_file_name)
            monkeypatch.chdir(temp_dir)
            assert len(relative) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH, (
                "the relative path must be long enough to reach the prefix branch, got "
                f"{len(relative)}"
            )

            result = _read_manifests([relative])

            assert result.get(manifest_file_name) is not None
        finally:
            self._remove_long_tree(temp_dir)

    def test_long_dotdot_path_does_not_raise_bare_valueerror(
        self, temp_dir, test_manifest_one, monkeypatch
    ):
        """
        A long path containing '..' does not escape as a bare ValueError.

        _get_long_path_compatible_path rejects '..' in a long path, and that guard is aimed
        at callers that skipped resolution. This function is documented to raise
        NonValidInputError, and _attachment_download / _manifest_merge above it handle only
        that -- so the '..' has to be collapsed by abspath here, against the real cwd,
        rather than reaching the guard.
        """
        manifest_file_name = "manifest_dotdot"
        try:
            self._write_long_manifest(temp_dir, test_manifest_one, manifest_file_name)
            monkeypatch.chdir(temp_dir)
            # Detour through the parent and back, keeping the string over the limit.
            with_dotdot = os.path.join("d" * 120, "e" * 120, "..", "e" * 120, manifest_file_name)
            assert len(with_dotdot) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH

            # Reads successfully rather than raising at all -- abspath resolves the '..'
            # before the guard can see it.
            result = _read_manifests([with_dotdot])

            assert result.get(manifest_file_name) is not None
        finally:
            self._remove_long_tree(temp_dir)
