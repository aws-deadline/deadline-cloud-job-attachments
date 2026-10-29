# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

r"""
The session job attachments info file.

The Deadline Cloud worker agent writes this file to the session working directory when it syncs
the job attachments inputs of a session. It sets the `DEADLINE_JA_SESSION_INFO_FILE` environment
variable to the file's path for the session's environment and task actions. Scripts that run in
the session can read the file to find each job attachments root on the worker host and the output
directories under it. For example, a queue environment's `onExit` action can use it to copy
outputs somewhere.

The file is JSON, so scripts can read it without this library:

```json
{
  "version": "ja-session-info-1.0",
  "roots": [
    {
      "rootPath": "C:\\Users\\artist\\project",
      "rootPathFormat": "windows",
      "fileSystemLocationName": null,
      "localRootPath": "/sessions/session-0123/assetroot-abc",
      "outputRelativeDirectories": ["renders/beauty"],
      "localOutputDirectories": ["/sessions/session-0123/assetroot-abc/renders/beauty"]
    }
  ]
}
```

Every key is always present. The keys are:

- `version`: the format version.
- `roots`: one entry per job attachments root. It's an empty list when the job has no job
  attachments roots.
- `rootPath` and `rootPathFormat` (`"posix"` or `"windows"`): the root on the submitting host.
- `fileSystemLocationName`: the storage profile file system location of the root, or `null`.
- `localRootPath`: where the root is on the worker host.
- `outputRelativeDirectories`: the job's output directories relative to the root. They're
  normalized with `/` separators whatever the submitting host's path format is, so you can use
  them to build S3 keys or other portable paths. Output directories that are absolute or outside
  the root are left out.
- `localOutputDirectories`: the same output directories as absolute paths on the worker host.

Guarantees:

- **When the file exists.** The worker agent writes the file during the job's job attachments
  input sync. That sync runs before the session's first environment or task action. The file
  doesn't change for the rest of the session. If `DEADLINE_JA_SESSION_INFO_FILE` isn't set, the
  session has no job attachments, or its worker agent is too old to write the file.
- **Output directories might not exist.** Directories in `localOutputDirectories` exist only once
  something creates them, usually the job's tasks.
- **Trust.** The file is in the session directory, which the job user can write to. Treat its
  contents as no more trustworthy than the job itself.
- **Compatibility.** Readers accept every `1.x` version and ignore keys they don't recognize.
  Later minor versions only add keys. A change that would break existing readers gets a new
  major version.
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
