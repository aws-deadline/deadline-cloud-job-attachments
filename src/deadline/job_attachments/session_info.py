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
      "outputRelativeDirectories": ["renders/beauty"],
      "localOutputDirectories": ["/sessions/session-0123/assetroot-abc/renders/beauty"]
    }
  ]
}
```

`rootPath` and `rootPathFormat` are the root on the submitting host. `localRootPath` is where that
root is on the worker host. `outputRelativeDirectories` are relative to the root, in the root's
path format, so a job submitted from Windows has backslash separators. `localOutputDirectories`
are the same directories as absolute paths on the worker host; scripts that don't use this library
should use them. Output directories that are absolute or outside the root are left out of
`localOutputDirectories`. `fileSystemLocationName` is present when the root belongs to a storage
profile file system location. `roots` is empty when the job has no job attachments roots.

Readers accept files with the same major version and ignore keys they don't recognize, so minor
versions can add keys without breaking existing scripts.
"""

__all__ = [
    "SESSION_INFO_ENV_VAR",
    "SESSION_INFO_FILE_NAME",
    "SESSION_INFO_VERSION",
    "SessionAssetRoot",
    "SessionJobAttachmentsInfo",
    "read_session_info",
    "write_session_info",
]

from ._session_info import (
    SESSION_INFO_ENV_VAR,
    SESSION_INFO_FILE_NAME,
    SESSION_INFO_VERSION,
    SessionAssetRoot,
    SessionJobAttachmentsInfo,
    read_session_info,
    write_session_info,
)
