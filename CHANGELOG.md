## 0.0.2 (2026-04-29)

### Features
* Added glob-style include filtering support for output downloads. You can now pass `include_filters` to `OutputDownloader` to selectively download only files matching specified patterns, and use `apply_include_filters()` to chain filters. (#21)
## 0.0.1 (2026-04-23)

Initial release of `deadline-job-attachments`, migrated from [aws-deadline/deadline-cloud@`83a363d8`](https://github.com/aws-deadline/deadline-cloud/commit/83a363d8).

### Features
* Path mapping now supports both boto3 response dicts and dataclass representations of storage profiles, providing more flexibility when working with storage profile path mapping. (#11, `fff7336`)

### Bug Fixes
* Fixed dependency job attachment syncing to use OVERWRITE mode instead of COPY mode. Previously, syncing dependency job attachments within the worker agent would incorrectly create new filenames with suffixes like `(1)` instead of overwriting existing files. (#11, `855d54f`)
* Improved the error message when a download directory cannot be created (e.g., due to cross-OS path incompatibility like Linux paths on macOS). Instead of a cryptic OSError, a clear message now explains the failure and suggests re-running the download to choose a valid local path. (#11, `92fc878`)

