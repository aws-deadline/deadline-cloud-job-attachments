# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from pathlib import Path
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from typing import List, Set, Union

from .exceptions import AssetSyncError, PathOutsideDirectoryError
from ._utils import (
    _get_long_path_compatible_path,
    _is_normalized_subpath,
    _normalize_windows_path,
)


@dataclass
class PosixFileSystemPermissionSettings:
    """
    A dataclass representing file system permission-related information
    for Posix. The specified permission modes will be bitwise-OR'ed with
    the directory or file's existing permissions.

    Attributes:
        os_user (str): The target operating system user for ownership.
        os_group (str): The target operating system group for ownership.
        dir_mode (int): The permission mode to be added to directories.
        file_mode (int): The permission mode to be added to files.
    """

    os_user: str
    os_group: str
    dir_mode: int
    file_mode: int


@dataclass
class WindowsPermissionEnum(Enum):
    """
    An enumeration of different Windows permission flags.
    """

    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    READ_WRITE = "READ_WRITE"
    FULL_CONTROL = "FULL_CONTROL"


@dataclass
class WindowsFileSystemPermissionSettings:
    """
    A dataclass representing file system permission-related information
    for Windows.

    Attributes:
        os_user (str): The target operating system user or ownership.
        os_group (str): The target operating system group for ownership.
        dir_mode (WindowsPermissionEnum): The permission mode to be added to directories.
        file_mode (WindowsPermissionEnum): The permission mode to be added to files.
    """

    os_user: str
    dir_mode: WindowsPermissionEnum
    file_mode: WindowsPermissionEnum


# A union of different file system permission settings that are based on the underlying OS.
FileSystemPermissionSettings = Union[
    PosixFileSystemPermissionSettings, WindowsFileSystemPermissionSettings
]


def _set_fs_group_for_posix(
    file_paths: List[str],
    local_root: str,
    fs_permission_settings: PosixFileSystemPermissionSettings,
) -> None:
    os_group = fs_permission_settings.os_group
    dir_mode = fs_permission_settings.dir_mode
    file_mode = fs_permission_settings.file_mode

    # A set that stores the unique directory paths where permissions need to be changed.
    dir_paths_to_change_fs_group: Set[Path] = set()

    # 1. Set group ownership and permissions for each file.
    for file_path_str in file_paths:
        # The file path must be relative to the root path (ie. local_root).
        if not _is_normalized_subpath(file_path_str, local_root):
            raise PathOutsideDirectoryError(
                f"The provided path '{file_path_str}' is not under the root directory: {local_root}"
            )

        _change_permission_for_posix(file_path_str, os_group, file_mode)

        # Add the parent directories of each file to the set of directories whose
        # group ownership and permissions will be changed.
        path_components = Path(file_path_str).relative_to(local_root).parents
        for path_component in path_components:
            path_to_change = Path(local_root).joinpath(path_component)
            dir_paths_to_change_fs_group.add(path_to_change)

    # 2. Set group ownership and permissions for the directories in the path starting from root.
    for dir_path in dir_paths_to_change_fs_group:
        _change_permission_for_posix(str(dir_path), os_group, dir_mode)


def _set_fs_permission_for_windows(
    file_paths: List[str],
    local_root: str,
    fs_permission_settings: WindowsFileSystemPermissionSettings,
) -> None:
    os_user = fs_permission_settings.os_user
    dir_mode = fs_permission_settings.dir_mode
    file_mode = fs_permission_settings.file_mode

    # A set that stores the unique directory paths where permissions need to be changed.
    dir_paths_to_change_fs_group: Set[Path] = set()

    # 1. Set permissions for each file.
    for file_path_str in file_paths:
        # The file path must be relative to the root path (ie. local_root).
        if not _is_normalized_subpath(file_path_str, local_root):
            raise PathOutsideDirectoryError(
                f"The provided path '{file_path_str}' is not under the root directory: {local_root}"
            )

        # Prefixed rather than trusting the caller to have done it: the paths from
        # _download_files_parallel arrive prefixed, but _set_fs_group is also called with
        # plain paths (download.py:1215,1223,1228). The helper is idempotent, so this is a
        # no-op for the already-prefixed ones. The unprefixed string is kept for the
        # containment check and error message above, which are string comparisons.
        _change_permission_for_windows(
            str(_get_long_path_compatible_path(file_path_str)), os_user, file_mode
        )

        # Add the parent directories of each file to the set of directories whose
        # permissions will be changed.
        path_components = (
            _normalize_windows_path(file_path_str)
            .relative_to(_normalize_windows_path(local_root))
            .parents
        )
        for path_component in path_components:
            path_to_change = Path(local_root).joinpath(path_component)
            dir_paths_to_change_fs_group.add(path_to_change)

    # 2. Set permissions for the directories in the path starting from root.
    for dir_path in dir_paths_to_change_fs_group:
        # Prefixed for the same reason the file paths in pass 1 arrive prefixed:
        # GetFileSecurity/SetFileSecurity go through the Win32 normalization that the
        # \\?\ prefix exists to bypass. These paths are rebuilt from the *unprefixed*
        # local_root, so without this the deeper directories under a >MAX_PATH download
        # root exceed the limit and the win32 call fails as an AssetSyncError.
        _change_permission_for_windows(
            str(_get_long_path_compatible_path(dir_path)), os_user, dir_mode
        )


def _change_permission_for_posix(
    path_str: str,
    os_group: str,
    mode: int,
) -> None:
    if sys.platform == "win32":
        raise EnvironmentError("This function can only be executed on POSIX systems.")

    path = Path(path_str)
    shutil.chown(path, group=os_group)
    os.chmod(path, path.stat().st_mode | mode)


def _change_permission_for_windows(
    path: str,
    os_user: str,
    mode: WindowsPermissionEnum,
) -> None:
    if sys.platform != "win32":
        raise EnvironmentError("This function can only be executed on Windows systems.")

    import win32security

    try:
        con_mode = _get_ntsecuritycon_mode(mode)
        # Lookup the user's SID (Security Identifier)
        user_sid = win32security.LookupAccountName(None, os_user)[0]
        # Get existing DACL (Discretionary Access Control List). If dacl is none, create a new one.
        sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            dacl = win32security.ACL()
        # Add new ACE (Access Control Entry)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, con_mode, user_sid)
        # Set the modified DACL to the security descriptor
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION, sd)
    except win32security.error as e:
        raise AssetSyncError(
            f"Failed to set permissions for file or directory ({path}): {e}"
        ) from e


def _get_ntsecuritycon_mode(mode: WindowsPermissionEnum) -> int:
    """
    Get the NTSecurityCon mode for a WindowsPermissionEnum.
    """
    if sys.platform != "win32":
        raise EnvironmentError("This function can only be executed on Windows systems.")

    import ntsecuritycon as con

    permission_mapping = {
        WindowsPermissionEnum.READ.value: con.FILE_GENERIC_READ,
        WindowsPermissionEnum.WRITE.value: con.FILE_GENERIC_WRITE,
        WindowsPermissionEnum.EXECUTE.value: con.FILE_GENERIC_EXECUTE,
        WindowsPermissionEnum.READ_WRITE.value: con.FILE_GENERIC_READ | con.FILE_GENERIC_WRITE,
        WindowsPermissionEnum.FULL_CONTROL.value: con.FILE_ALL_ACCESS,
    }
    return permission_mapping[mode.value]
