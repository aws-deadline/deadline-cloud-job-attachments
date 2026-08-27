# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
from deadline.job_attachments._utils import WINDOWS_UNC_PATH_STRING_PREFIX
from deadline.job_attachments.exceptions import NonValidInputError
import pytest
from typing import List
from deadline.job_attachments._glob import _glob_paths, _process_glob_inputs


def test_glob_inputs_string(glob_config_file):
    """
    Test case to test glob config as a string.
    """
    glob: str
    with open(glob_config_file) as f:
        glob = f.read()
    glob_config = _process_glob_inputs(glob)
    assert "include.file" in glob_config.include_glob
    assert "exclude.file" in glob_config.exclude_glob


def test_glob_inputs_file(glob_config_file):
    """
    Test case to test glob config as a file.
    """
    glob_config = _process_glob_inputs(glob_config_file)
    assert "include.file" in glob_config.include_glob
    assert "exclude.file" in glob_config.exclude_glob


def test_bad_glob_string():
    """
    Test case to test a bad glob config will raise an exception.
    """
    glob: str = "This is not a json"
    with pytest.raises(NonValidInputError):
        _process_glob_inputs(glob)


def test_glob_path_default(test_glob_folder: str):
    """
    Test case to glob all files.
    """
    globbed_files: List[str] = _glob_paths(path=test_glob_folder)

    # There are 4 files
    assert len(globbed_files) == 4
    assert os.path.join(os.sep, test_glob_folder, "include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "exclude.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_exclude.txt") in globbed_files


def test_glob_path_default_include(test_glob_folder: str):
    """
    Test case to glob all files.
    """
    globbed_files: List[str] = _glob_paths(
        path=test_glob_folder, include=["*include.txt", "*/*include.txt"]
    )

    # There are 2 files
    assert len(globbed_files) == 2
    assert os.path.join(os.sep, test_glob_folder, "include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_include.txt") in globbed_files


def test_glob_path_exclude(test_glob_folder: str):
    """
    Test case to glob all files and exclude some.
    """
    globbed_files: List[str] = _glob_paths(
        path=test_glob_folder, exclude=["*exclude.txt", "*/*exclude.txt"]
    )

    # There are 4 files
    assert len(globbed_files) == 2
    assert os.path.join(os.sep, test_glob_folder, "include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_include.txt") in globbed_files


def test_glob_path_include_subdir(test_glob_folder: str):
    """
    Test case to glob files only from the include sub directory.
    """
    globbed_files: List[str] = _glob_paths(path=test_glob_folder, include=["nested/**"])

    # There are 2 files
    assert len(globbed_files) == 2
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "nested", "nested_exclude.txt") in globbed_files


def test_glob_path_include_nonexistent(test_glob_folder: str):
    """
    Test case to glob files only from the include sub directory which does not exist.
    """
    globbed_files: List[str] = _glob_paths(path=test_glob_folder, include=["nonexistent/**"])

    # There are 0 files
    assert len(globbed_files) == 0


def test_glob_path_exclude_subdir(test_glob_folder: str):
    """
    Test case to glob files and exclude sub directory.
    """
    globbed_files: List[str] = _glob_paths(path=test_glob_folder, exclude=["nested/**"])

    # There are 2 files
    assert len(globbed_files) == 2
    assert os.path.join(os.sep, test_glob_folder, "include.txt") in globbed_files
    assert os.path.join(os.sep, test_glob_folder, "exclude.txt") in globbed_files


def test_glob_path_exclude_nonexistent(test_glob_folder: str):
    """
    Test case to glob files only exclude sub directory which does not exist.
    """
    globbed_files: List[str] = _glob_paths(path=test_glob_folder, exclude=["nonexistent/**"])

    # There are 2 files
    assert len(globbed_files) == 4


class TestGlobPathsWalkPrefix:
    """
    Pins the walk-root prefixing added to fix the silent-empty-manifest failure on
    non-longPathAware hosts: the walk must go through the \\\\?\\ form so glob.glob can
    descend past MAX_PATH, and returned paths must be plain so downstream callers keep
    them as manifest keys and containment inputs and re-apply the prefix per file.

    Uses a mocked glob so the test does not depend on the host's actual long-path
    support -- the string construction is what is being pinned here.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows walk-root prefixing")
    def test_walk_root_is_prefixed_and_returned_paths_are_plain(self, monkeypatch, tmp_path):
        seen_patterns: List[str] = []

        def fake_glob(pattern: str, recursive: bool = False) -> List[str]:
            seen_patterns.append(pattern)
            # Model what glob actually yields under a prefixed walk root: paths that
            # carry the same prefix. Anything before "**" is the walked base.
            base = pattern.split("**")[0].rstrip("\\/")
            return [base + os.sep + "asset.txt"]

        monkeypatch.setattr("deadline.job_attachments._glob.glob.glob", fake_glob)
        monkeypatch.setattr("deadline.job_attachments._glob.os.path.isfile", lambda p: True)

        result = _glob_paths(str(tmp_path))

        assert seen_patterns, "glob.glob was not called"
        assert all(p.startswith(WINDOWS_UNC_PATH_STRING_PREFIX) for p in seen_patterns), (
            f"walk root not prefixed: {seen_patterns!r}"
        )
        for path in result:
            assert not path.startswith(WINDOWS_UNC_PATH_STRING_PREFIX), (
                f"returned path still carries the prefix: {path!r}"
            )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX sanity: no prefixing")
    def test_walk_and_returned_paths_are_untouched_on_posix(self, monkeypatch, tmp_path):
        seen_patterns: List[str] = []

        def fake_glob(pattern: str, recursive: bool = False) -> List[str]:
            seen_patterns.append(pattern)
            base = pattern.split("**")[0].rstrip("/")
            return [base + "/asset.txt"]

        monkeypatch.setattr("deadline.job_attachments._glob.glob.glob", fake_glob)
        monkeypatch.setattr("deadline.job_attachments._glob.os.path.isfile", lambda p: True)

        result = _glob_paths(str(tmp_path))

        assert seen_patterns, "glob.glob was not called"
        # POSIX must not touch the Windows prefix machinery on either side.
        assert all("\\\\?\\" not in p for p in seen_patterns)
        for path in result:
            assert "\\\\?\\" not in path
