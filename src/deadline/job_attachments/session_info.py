# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
The session job attachments info file.

After the Deadline Cloud worker agent syncs the job attachments inputs of a session, it writes this
file to the session working directory and sets the `DEADLINE_JA_SESSION_INFO_FILE` environment
variable to its path for the session's environment and task actions. Scripts that run in the
session, such as a queue environment's `onExit` action, can read it to find where each job
attachments root is on the worker host and which directories under it are output directories.

The file is JSON, so scripts can read it without this library:

```json
{
  "version": "jobattachments-session-info-1.0",
  "roots": [
    {
      "rootPath": "/home/artist/project",
      "rootPathFormat": "posix",
      "localRootPath": "/sessions/session-0123/assetroot-abc",
      "outputRelativeDirectories": ["renders/beauty"]
    }
  ]
}
```

`rootPath` and `rootPathFormat` are the root on the submitting host. `localRootPath` is where that
root is on the worker host. `outputRelativeDirectories` are relative to the root, in the root's
path format. `fileSystemLocationName` is present when the root belongs to a storage profile file
system location.

Readers accept files with the same major version and ignore keys they don't recognize, so minor
versions can add keys without breaking existing scripts.
"""

from __future__ import annotations

import json as _json
import os as _os
from dataclasses import dataclass, field
from pathlib import Path as _Path
from pathlib import PurePosixPath as _PurePosixPath
from pathlib import PureWindowsPath as _PureWindowsPath
from typing import Any as _Any
from typing import Dict as _Dict
from typing import List as _List
from typing import Optional as _Optional
from typing import Union as _Union

from .models import ManifestProperties as _ManifestProperties
from .models import PathFormat as _PathFormat

__all__ = [
    "SESSION_INFO_ENV_VAR",
    "SESSION_INFO_FILE_NAME",
    "SESSION_INFO_VERSION",
    "SessionAssetRoot",
    "SessionJobAttachmentsInfo",
    "read_session_info",
    "write_session_info",
]

SESSION_INFO_ENV_VAR = "DEADLINE_JA_SESSION_INFO_FILE"
"""The environment variable that holds the path of the session job attachments info file."""

SESSION_INFO_FILE_NAME = "job_attachments_session_info.json"
"""The name of the session job attachments info file in the session working directory."""

SESSION_INFO_VERSION = "jobattachments-session-info-1.0"
"""The version of the session job attachments info file format that this library writes."""

_VERSION_PREFIX = "jobattachments-session-info-"


@dataclass
class SessionAssetRoot:
    """A job attachments root and where it is on the worker host."""

    root_path: str
    """The root path on the submitting host."""
    root_path_format: _PathFormat
    """The path format of the submitting host."""
    local_root_path: str
    """The path of the root on the worker host."""
    file_system_location_name: _Optional[str] = None
    """The storage profile file system location of the root, if it has one."""
    output_relative_directories: _List[str] = field(default_factory=list)
    """The output directories of the job, relative to the root, in the root's path format."""

    @classmethod
    def from_manifest_properties(
        cls, manifest_properties: _ManifestProperties, local_root_path: str
    ) -> SessionAssetRoot:
        """Create a session asset root from a job's manifest properties and its local root path."""
        return cls(
            root_path=manifest_properties.rootPath,
            root_path_format=manifest_properties.rootPathFormat,
            local_root_path=local_root_path,
            file_system_location_name=manifest_properties.fileSystemLocationName,
            output_relative_directories=list(manifest_properties.outputRelativeDirectories or []),
        )

    def local_output_directories(self) -> _List[str]:
        """Return the absolute paths of the output directories on the worker host."""
        pure_path = (
            _PureWindowsPath if self.root_path_format == _PathFormat.WINDOWS else _PurePosixPath
        )
        return [
            _os.path.normpath(_os.path.join(self.local_root_path, *pure_path(directory).parts))
            for directory in self.output_relative_directories
        ]

    def to_dict(self) -> _Dict[str, _Any]:
        result: _Dict[str, _Any] = {
            "rootPath": self.root_path,
            "rootPathFormat": self.root_path_format.value,
            "localRootPath": self.local_root_path,
            "outputRelativeDirectories": list(self.output_relative_directories),
        }
        if self.file_system_location_name:
            result["fileSystemLocationName"] = self.file_system_location_name
        return result

    @classmethod
    def from_dict(cls, data: _Dict[str, _Any]) -> SessionAssetRoot:
        return cls(
            root_path=data["rootPath"],
            root_path_format=_PathFormat(data["rootPathFormat"]),
            local_root_path=data["localRootPath"],
            file_system_location_name=data.get("fileSystemLocationName"),
            output_relative_directories=list(data.get("outputRelativeDirectories") or []),
        )


@dataclass
class SessionJobAttachmentsInfo:
    """The job attachments roots of a session."""

    roots: _List[SessionAssetRoot] = field(default_factory=list)

    def to_dict(self) -> _Dict[str, _Any]:
        return {
            "version": SESSION_INFO_VERSION,
            "roots": [root.to_dict() for root in self.roots],
        }

    @classmethod
    def from_dict(cls, data: _Dict[str, _Any]) -> SessionJobAttachmentsInfo:
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
            return cls(roots=[SessionAssetRoot.from_dict(root) for root in data["roots"]])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed session job attachments info: {e}") from e


def write_session_info(
    info: SessionJobAttachmentsInfo, session_dir: _Union[str, _os.PathLike]
) -> _Path:
    """
    Write the session info to the session working directory.

    The file is replaced atomically, so a reader never sees a partially written file.

    Returns:
        The path of the file that was written.
    """
    path = _Path(session_dir) / SESSION_INFO_FILE_NAME
    temp_path = path.with_name(f".{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        _json.dump(info.to_dict(), f, indent=2)
    _os.replace(temp_path, path)
    return path


def read_session_info(
    path: _Optional[_Union[str, _os.PathLike]] = None,
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
        env_path = _os.environ.get(SESSION_INFO_ENV_VAR)
        if not env_path:
            raise ValueError(
                f"The {SESSION_INFO_ENV_VAR} environment variable isn't set. The session either"
                " has no job attachments or runs on a worker agent that doesn't write the file."
            )
        path = env_path
    with open(path, encoding="utf-8") as f:
        try:
            data = _json.load(f)
        except ValueError as e:
            raise ValueError(f"The session job attachments info file isn't valid JSON: {e}") from e
    return SessionJobAttachmentsInfo.from_dict(data)
