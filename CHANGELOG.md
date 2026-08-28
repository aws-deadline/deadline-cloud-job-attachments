## 0.1.4 (2026-08-27)

### Bug Fixes
* Fixed handling of Windows long paths (>260 characters) during output sync. Paths that exceed the MAX_PATH limit are now correctly prefixed to avoid file operation failures. (#68)
* The `\\?\` long path prefix is now applied regardless of the Windows `LongPathsEnabled` registry setting. Previously, the prefix was only added when the registry key was set, which meant it was skipped for DCC-hosted Python interpreters that don't declare `longPathAware` in their application manifest. The UNC path form has also been corrected. (#67)
## 0.1.3 (2026-07-15)

### Features
* Improved upload performance on Windows by caching the long-path registry check (avoiding repeated ctypes allocations) and deduplicating stat calls per file, ensuring consistent metadata between the hash cache and manifest. (`c9868d6`)

### Bug Fixes
* Fixed support for Windows long paths (>= MAX_PATH / 260 characters) in the upload flow. Files with long paths are now correctly resolved and uploaded. (`e75531b`)
## 0.1.2 (2026-07-03)
## 0.1.1 (2026-06-18)

### Features
* Improved S3 download and upload performance by setting `response_checksum_validation` to `when_required`, reducing unnecessary checksum validation overhead during file transfers. (#37)
## 0.1.0 (2026-05-01)

### BREAKING CHANGES
* `OutputDownloader` methods have been renamed as part of unifying the downloader API to also support downloading inputs. (#33)
    * download_job_output() → download()
    * get_output_paths_by_root() → get_paths_by_root()
    * outputs_by_root → paths_by_root
* get_sts_client() and get_caller_identity() have been removed (#31)

### Features
* Added a new `InputDownloader` class for downloading job input files with glob-style include filtering, mirroring the existing `OutputDownloader` capability. (#33)
* STS is no longer a required dependency, but may be used on older credentials providers when no account ID is provided. If your code relied on the previous STS-based behavior, no action is needed — the fallback still works — but you may be able to remove the STS endpoint for credential providers that already know the account (e.g., AssumeRole, static credentials with `AWS_ACCOUNT_ID`). (#31)


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

