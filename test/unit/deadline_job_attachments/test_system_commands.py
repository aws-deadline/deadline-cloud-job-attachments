# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the trusted-path command resolver.

These pin the security properties of ``_system_commands``. Each has been
mutation-checked: reverting the corresponding production behaviour makes a named
test here fail. See that module's docstring for why each property matters.
"""

from __future__ import annotations

import os
import posixpath
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from deadline.job_attachments._system_commands import (
    SystemCommandNotFoundError,
    TRUSTED_SYSTEM_DIRECTORIES,
    find_system_command,
    system_command_path,
)

_MODULE = "deadline.job_attachments._system_commands"


@pytest.fixture
def executable_dir(tmp_path: Path) -> Path:
    """A directory containing an executable file named ``target-cmd``."""
    target = tmp_path / "target-cmd"
    target.write_text("#!/bin/sh\ntrue\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


class TestOnlySearchesTrustedDirectories:
    def test_resolves_a_command_in_a_searched_directory(self, executable_dir: Path) -> None:
        """The negative control. Without it, the "not found" assertions below
        would be indistinguishable from a resolver that never finds anything."""
        # GIVEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(executable_dir),)):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result == str(executable_dir / "target-cmd")

    def test_does_not_resolve_a_command_outside_searched_directories(
        self, executable_dir: Path
    ) -> None:
        # GIVEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", ("/usr/bin", "/bin")):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None

    def test_ignores_path_even_when_it_contains_a_matching_command(
        self, executable_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins "PATH is never read".

        This is the property a ``shutil.which`` implementation would silently
        violate, and the one a PATH fallback for missing commands would undo.
        """
        # GIVEN
        monkeypatch.setenv("PATH", str(executable_dir))
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", ("/usr/bin", "/bin")):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None, "PATH was consulted"

    def test_returns_the_first_matching_directory(self, tmp_path: Path) -> None:
        """Order is load-bearing: on NixOS the setuid sudo wrapper must win over a
        non-setuid /usr/bin copy."""
        # GIVEN
        first, second = tmp_path / "first", tmp_path / "second"
        for directory in (first, second):
            directory.mkdir()
            target = directory / "target-cmd"
            target.write_text("#!/bin/sh\ntrue\n")
            target.chmod(target.stat().st_mode | stat.S_IXUSR)

        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(first), str(second))):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result == str(first / "target-cmd")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="On Windows os.access(X_OK) is true for any existing file, so "
        "'not executable' is not expressible there",
    )
    def test_ignores_a_non_executable_file(self, tmp_path: Path) -> None:
        # GIVEN
        (tmp_path / "target-cmd").write_text("not executable")

        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(tmp_path),)):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None


class TestRejectsNonBareNames:
    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("a/b", id="forward-slash"),
            pytest.param("a\\b", id="backslash"),
            pytest.param("", id="empty"),
            pytest.param(".", id="curdir"),
            pytest.param("..", id="pardir"),
            # ntpath.join(r"C:\Windows\System32", "D:evil") == "D:evil" -- a
            # drive-relative name discards the trusted prefix while containing no
            # separator, so a separator-only guard lets it through.
            pytest.param("D:evil", id="drive-relative"),
            pytest.param("a:b", id="colon"),
        ],
    )
    def test_rejects_name_with_a_path_component(self, name: str) -> None:
        with pytest.raises(ValueError):
            find_system_command(name)

    def test_rejects_traversal_even_though_the_target_is_reachable(self, tmp_path: Path) -> None:
        """The guard must be about the name, not about whether the join happens to
        land on a real file -- so prove the target IS reachable by that join
        before asserting the name is refused.

        Without this, a test whose searched directory has nothing above it would
        pass even with the guard deleted, pinning nothing.
        """
        # GIVEN
        target = tmp_path / "target-cmd"
        target.write_text("#!/bin/sh\ntrue\n")
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        nested = tmp_path / "nested"
        nested.mkdir()
        assert os.path.isfile(os.path.join(str(nested), "../target-cmd")), (
            "precondition: the traversal target is reachable by this join"
        )

        # WHEN / THEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(nested),)):
            with pytest.raises(ValueError, match="path separator"):
                find_system_command("../target-cmd")

    def test_rejection_is_valueerror_not_notfound(self) -> None:
        """A bad name is a caller bug; a missing command is an environment
        problem. Conflating them would let a caller mistake one for the other."""
        with pytest.raises(ValueError):
            system_command_path("a/b")


class TestMissingCommandRaises:
    def test_find_returns_none(self) -> None:
        assert find_system_command("deadline-definitely-not-installed") is None

    def test_raises_rather_than_returning_the_bare_name(self) -> None:
        """The silent-fallback failure mode: returning "sudo" here would look
        fixed and behave exactly as the vulnerability did."""
        with pytest.raises(SystemCommandNotFoundError) as excinfo:
            system_command_path("deadline-definitely-not-installed")

        message = str(excinfo.value)
        assert "deadline-definitely-not-installed" in message
        assert "PATH is deliberately not searched" in message

    def test_is_not_a_filenotfounderror(self) -> None:
        """vfs already uses FileNotFoundError to mean "the VFS pid file is missing".

        Three handlers in that module catch it for that purpose, and one of them
        wraps a call chain reaching this resolver: kill_all_processes ->
        shutdown_libfuse_mount -> wait_for_mount -> is_mount. Inheriting from
        FileNotFoundError let a resolution failure be logged as a missing pid file
        and skip the os.remove that follows, leaving a stale file behind.

        Note this is the opposite of the right answer in the openjd-sessions
        resolver, whose cancel path deliberately catches OSError so that a failed
        signal cannot unwind a cancelation. The base class follows the handlers that
        surround each one, so the two differ on purpose.
        """
        assert not issubclass(SystemCommandNotFoundError, FileNotFoundError)
        assert not issubclass(SystemCommandNotFoundError, OSError)

    def test_vfs_translates_it_into_the_type_callers_handle(self) -> None:
        """asset_sync guards the mount paths with `except VFSExecutableMissingError`
        and nothing else, so an untranslated resolver failure would bypass its
        fallback to a copy-based sync instead of triggering it."""
        # GIVEN
        from deadline.job_attachments.exceptions import VFSExecutableMissingError
        from deadline.job_attachments.vfs import VFSProcessManager

        # WHEN / THEN
        with patch(
            "deadline.job_attachments.vfs._system_command_path",
            side_effect=SystemCommandNotFoundError("no sudo here"),
        ):
            with pytest.raises(VFSExecutableMissingError) as excinfo:
                VFSProcessManager._resolve_or_raise("sudo")

        assert "no sudo here" in str(excinfo.value)


class TestTrustedDirectories:
    def test_all_entries_are_absolute(self) -> None:
        """A relative entry would resolve against the process working directory.

        Checked with posixpath rather than os.path on purpose. These entries are
        POSIX paths -- the VFS this module serves is POSIX-only -- and from Python
        3.13 ntpath.isabs() no longer calls a single-slash path absolute, treating
        it as drive-relative instead. Using os.path made this assertion a statement
        about the host running the tests rather than about the constant, and it
        failed the windows-latest 3.13 leg for that reason.
        """
        for directory in TRUSTED_SYSTEM_DIRECTORIES:
            assert posixpath.isabs(directory), f"{directory} is not absolute"

    def test_searches_the_setuid_wrapper_directory_before_usr_bin(self) -> None:
        assert TRUSTED_SYSTEM_DIRECTORIES.index(
            "/run/wrappers/bin"
        ) < TRUSTED_SYSTEM_DIRECTORIES.index("/usr/bin")

    def test_searches_both_sbin_locations(self) -> None:
        """On non-usr-merged distributions some commands exist only under /sbin."""
        assert "/usr/sbin" in TRUSTED_SYSTEM_DIRECTORIES
        assert "/sbin" in TRUSTED_SYSTEM_DIRECTORIES

    def test_the_two_nixos_entries_are_present_as_a_pair(self) -> None:
        """/run/wrappers/bin alone supports no complete code path: it holds only the
        setuid wrappers, so on NixOS it resolves sudo and nothing else. findmnt is in
        the sw/bin symlink farm, so without that entry the ordering would resolve
        sudo and then fail on findmnt."""
        assert "/run/wrappers/bin" in TRUSTED_SYSTEM_DIRECTORIES
        assert "/run/current-system/sw/bin" in TRUSTED_SYSTEM_DIRECTORIES


@pytest.mark.skipif(sys.platform == "win32", reason="VFS doesn't currently support Windows")
class TestGetShutdownArgsFailureContract:
    """`get_shutdown_args` must answer a missing binary with None, not an exception.

    It is typed `Optional[list]` and already warns-and-returns-None for a missing
    fusermount3. Its caller, `shutdown_libfuse_mount`, has no except clause for this
    function -- only a falsy check -- so an exception here escapes the unmount
    cleanup path instead of letting it fall through to `wait_for_mount`.

    Pinned because the obvious refactor (use the raising resolver everywhere, for
    consistency with the launch path) silently introduces that second failure mode.
    """

    def test_returns_none_when_sudo_is_not_in_a_trusted_directory(self) -> None:
        # GIVEN
        from deadline.job_attachments.vfs import VFSProcessManager

        # Nested rather than a parenthesized group: this package supports Python
        # 3.8, where `with (a, b):` is not valid syntax.
        with patch(
            "deadline.job_attachments.vfs.VFSProcessManager.find_vfs_link_dir",
            return_value="/some/link/dir",
        ):
            with patch("deadline.job_attachments.vfs.os.path.exists", return_value=True):
                with patch("deadline.job_attachments.vfs._find_system_command", return_value=None):
                    # WHEN
                    result = VFSProcessManager.get_shutdown_args("/mnt/point", "job-user")

        # THEN
        assert result is None

    def test_returns_argv_when_sudo_is_present(self) -> None:
        """Negative control: the None above must be about sudo being absent, not
        about this path being broken outright."""
        # GIVEN
        from deadline.job_attachments.vfs import VFSProcessManager

        with patch(
            "deadline.job_attachments.vfs.VFSProcessManager.find_vfs_link_dir",
            return_value="/some/link/dir",
        ):
            with patch("deadline.job_attachments.vfs.os.path.exists", return_value=True):
                with patch(
                    "deadline.job_attachments.vfs._find_system_command",
                    return_value="/trusted/sudo",
                ):
                    # WHEN
                    result = VFSProcessManager.get_shutdown_args("/mnt/point", "job-user")

        # THEN
        assert result is not None
        assert result[0] == "/trusted/sudo"
        assert "job-user" in result
        assert result[-1] == "/mnt/point"
