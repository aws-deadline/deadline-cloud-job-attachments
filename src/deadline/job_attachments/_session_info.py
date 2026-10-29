# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Implementation of the session job attachments info file. See `deadline.job_attachments.session_info`."""

from __future__ import annotations

import json
import os
import posixpath
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Union

from .models import ManifestProperties, PathFormat

SESSION_INFO_ENV_VAR = "DEADLINE_JA_SESSION_INFO_FILE"
"""The environment variable that holds the path of the session job attachments info file."""

SESSION_INFO_FILE_NAME = "job_attachments_session_info.json"
"""The name of the session job attachments info file in the session working directory."""

SESSION_INFO_VERSION = "ja-session-info-1.0"
"""The version of the session job attachments info file format that this library writes."""

_VERSION_PREFIX = "ja-session-info-"


@dataclass
class SessionAssetRoot:
    """A job attachments root and where it is on the worker host."""

    root_path: str
    """The root path on the submitting host."""
    root_path_format: PathFormat
    """The path format of the submitting host."""
    local_root_path: str
    """The path of the root on the worker host."""
    file_system_location_name: Optional[str] = None
    """The storage profile file system location of the root, if it has one."""
    output_relative_directories: List[str] = field(default_factory=list)
    """The output directories of the job, relative to the root, with `/` separators."""

    @classmethod
    def from_manifest_properties(
        cls, manifest_properties: ManifestProperties, local_root_path: str
    ) -> SessionAssetRoot:
        """
        Create a session asset root from a job's manifest properties and its local root path.

        The output directories are converted to `/` separators. Output directories that are
        absolute or outside the root are left out.
        """
        return cls(
            root_path=manifest_properties.rootPath,
            root_path_format=manifest_properties.rootPathFormat,
            local_root_path=local_root_path,
            file_system_location_name=manifest_properties.fileSystemLocationName,
            output_relative_directories=_normalize_relative_directories(
                manifest_properties.outputRelativeDirectories or [],
                manifest_properties.rootPathFormat,
            ),
        )

    def local_output_directories(self) -> List[str]:
        """Return the absolute paths of the output directories on the worker host."""
        local_root = os.path.normpath(self.local_root_path)
        return [
            os.path.normpath(os.path.join(local_root, *PurePosixPath(directory).parts))
            for directory in self.output_relative_directories
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rootPath": self.root_path,
            "rootPathFormat": self.root_path_format.value,
            "fileSystemLocationName": self.file_system_location_name,
            "localRootPath": self.local_root_path,
            "outputRelativeDirectories": list(self.output_relative_directories),
            "localOutputDirectories": self.local_output_directories(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionAssetRoot:
        """
        Create a session asset root from its JSON form.

        `localOutputDirectories` is derived from the other keys, so it's ignored. Output
        directories that are absolute or outside the root are left out.
        """
        root_path = _get_str(data, "rootPath")
        local_root_path = _get_str(data, "localRootPath")
        file_system_location_name = data.get("fileSystemLocationName")
        if file_system_location_name is not None and not isinstance(file_system_location_name, str):
            raise ValueError("fileSystemLocationName must be a string or null")
        output_relative_directories = data.get("outputRelativeDirectories", [])
        if not isinstance(output_relative_directories, list) or not all(
            isinstance(directory, str) for directory in output_relative_directories
        ):
            raise ValueError("outputRelativeDirectories must be a list of strings")
        return cls(
            root_path=root_path,
            root_path_format=PathFormat(data["rootPathFormat"]),
            local_root_path=local_root_path,
            file_system_location_name=file_system_location_name,
            output_relative_directories=_normalize_relative_directories(
                output_relative_directories, PathFormat.POSIX
            ),
        )


def _normalize_relative_directories(directories: List[str], path_format: PathFormat) -> List[str]:
    """
    Convert relative directories to normalized paths with `/` separators, leaving out directories
    that are absolute or outside the root.
    """
    pure_path = PureWindowsPath if path_format == PathFormat.WINDOWS else PurePosixPath
    result = []
    for directory in directories:
        relative = pure_path(directory)
        if relative.anchor:
            continue
        normalized = posixpath.normpath("/".join(relative.parts) or ".")
        if normalized == ".." or normalized.startswith("../"):
            continue
        result.append(normalized)
    return result


def _get_str(data: Dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


@dataclass
class SessionJobAttachmentsInfo:
    """The job attachments roots of a session."""

    roots: List[SessionAssetRoot] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SESSION_INFO_VERSION,
            "roots": [root.to_dict() for root in self.roots],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionJobAttachmentsInfo:
        """
        Create the session info from its JSON form.

        Raises:
            ValueError: If the data isn't a session info document this library can read.
        """
        if not isinstance(data, dict):
            raise ValueError("The session job attachments info must be a JSON object.")
        version = data.get("version")
        if not isinstance(version, str) or not version.startswith(_VERSION_PREFIX):
            raise ValueError(f"Unrecognized session job attachments info version: {version!r}")
        major_version = version[len(_VERSION_PREFIX) :].split(".")[0]
        supported_major_version = SESSION_INFO_VERSION[len(_VERSION_PREFIX) :].split(".")[0]
        if major_version != supported_major_version:
            raise ValueError(f"Unsupported session job attachments info version: {version!r}")
        try:
            roots = data["roots"]
            if not isinstance(roots, list):
                raise ValueError("roots must be a list")
            return cls(roots=[SessionAssetRoot.from_dict(root) for root in roots])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed session job attachments info: {e}") from e


def write_session_info(
    info: SessionJobAttachmentsInfo, session_dir: Union[str, os.PathLike]
) -> Path:
    """
    Write the session info to the session working directory.

    The file is replaced atomically, so a reader never sees a partially written file.

    Returns:
        The path of the file that was written.
    """
    path = Path(session_dir) / SESSION_INFO_FILE_NAME
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # O_EXCL refuses to follow a link planted at the temporary path. The file is readable by the
    # session directory's group so that the job user can read it.
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info.to_dict(), f, indent=2)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            # The original exception is more useful than a failure to clean up after it.
            pass
        raise
    return path


def read_session_info(
    path: Optional[Union[str, os.PathLike]] = None,
) -> SessionJobAttachmentsInfo:
    """
    Read the session info file.

    Args:
        path: The path of the file. Defaults to the value of the `DEADLINE_JA_SESSION_INFO_FILE`
            environment variable.

    Raises:
        ValueError: If no path is given and the environment variable isn't set, or the file isn't
            a session info document this library can read.
        OSError: If the file can't be read.
    """
    if path is None:
        env_path = os.environ.get(SESSION_INFO_ENV_VAR)
        if not env_path:
            raise ValueError(
                f"The {SESSION_INFO_ENV_VAR} environment variable isn't set. The session either"
                " has no job attachments or runs on a worker agent that doesn't write the file."
            )
        path = env_path
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as e:
            raise ValueError(f"The session job attachments info file isn't valid JSON: {e}") from e
    return SessionJobAttachmentsInfo.from_dict(data)
