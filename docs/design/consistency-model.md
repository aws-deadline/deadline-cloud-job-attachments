# Job Attachments Consistency Model

This document describes the consistency model of job attachments: what a task can rely on seeing when it reads job inputs and the outputs of other tasks, and what it can't. It has four parts:

- **Context**: why the model is shaped the way it is.
- **Invariants**: the guarantees, stated independently of how they're implemented.
- **Current implementation**: how the invariants are met today.
- **Known gaps**: places where today's implementation falls short of the invariants.

## Context

Job attachments loosely target the semantics of a typical render farm. On a render farm, workers read inputs from and write outputs to a shared filesystem. Nothing orders tasks that run at the same time, and when two tasks write the same file, whichever happens to write last wins.

Well-formed jobs write disjoint outputs, for example one frame per task. Two tasks that have no dependency between them overwriting the same path is a job-authoring error, not a supported pattern. Job attachments settle on one version, but don't promise which one.

Ordering is only guaranteed where the job structure states it:

- **Step dependencies.** A step that depends on another step sees all of that step's outputs.
- **Reruns of the same task.** A newer successful run of a task supersedes the older runs.

Keeping the model this loose means workers never coordinate with each other, there are no locks, and stored objects are written once. That lets job attachments scale with the size of the farm.

## Invariants

These describe behaviour, not mechanism. Any implementation of job attachments must preserve them.

- **Inputs before start.** A job's input files and the manifests that describe them are fully stored before any of its tasks can run.
- **Outputs before success.** A task run's output files and manifests are fully stored before the run is reported as succeeded.
- **Only successful runs contribute.** Failed or canceled task runs never make outputs visible.
- **Dependencies are visible.** A task sees the job's inputs plus the outputs of every task in each step it depends on.
- **Layering.** Where a path appears in both, dependency outputs override job inputs.
- **Reruns supersede.** For a given task, only the latest successful run's outputs are visible.
- **Immutability.** Stored files and manifests never change after they're written. New state is represented by new objects.
- **Additive outputs.** A task can add or modify files. It can't delete files that a downstream task would see.
- **No ordering between independent tasks.** If tasks with no dependency between them write the same path, the result is one of their versions, and which one is unspecified. This includes tasks within the same step, and outputs from separate steps that a task depends on.
- **Reader contract.** Consumers rely on a task run's outputs only after that run has succeeded.

## Current implementation

This section describes today's choices. They are subject to change, as long as the invariants above still hold.

### Storage layout

- **Files** are stored in a content-addressed store at `<rootPrefix>/Data/<hash>.<hashAlg>`.
- **Input manifests** are stored at `<rootPrefix>/Manifests/<farmId>/<queueId>/Inputs/<guid>/<rootPathHash>_input`. The `<guid>` is a random UUID generated for each input manifest at submission time. It isn't tied to the job, which doesn't exist yet. The job references the manifest through `inputManifestPath` in its attachments.
- **Output manifests** are stored at `<rootPrefix>/Manifests/<farmId>/<queueId>/<jobId>/<stepId>/<taskId>/<timestamp>_<sessionActionId>/<rootPathHash>_output`. Steps that use task chunking omit the `<taskId>` level. The timestamp is the session action's start time, in UTC, formatted as ISO 8601 with microseconds.
- **Asset roots** are recorded in the manifest object's S3 metadata (`asset-root` or `asset-root-json`).

See `JobAttachmentS3Settings` in `models.py` for the key builders.

### How each invariant is met

- **Inputs before start.** The client uploads input files and manifests (`_upload_attachments` in `deadline-cloud`) before it calls `CreateJob`. A job that doesn't exist yet can't be scheduled.
- **Outputs before success.** When a task run succeeds, the worker agent queues a `SYNC_OUTPUT_JOB_ATTACHMENTS` action at the front of the session's queue. It holds back the task run's status update until the upload finishes, then reports `SUCCEEDED` along with the output manifest paths and hashes. If the upload fails, the task run fails.
- **Only successful runs contribute.** The worker agent queues the output upload only when the task run's state is `SUCCESS`.
- **Dependencies are visible.** The scheduler doesn't start a dependent step until every task in the step it depends on has succeeded. The downstream task then lists that step's output manifest prefix in S3. This relies on S3 being strongly consistent for reads after writes and for listings. See `AssetSync._aggregate_asset_root_manifests` and `get_output_manifests_by_asset_root`.
- **Layering.** Manifests are merged in order: input manifests first, then each dependency step's output manifests. The merge is last-writer-wins by path (`merge_asset_manifests`).
- **Reruns supersede.** For each task, the reader takes the output manifests from the `<timestamp>_<sessionActionId>` folder that sorts last as a string (`_get_tasks_manifests_keys_from_s3`). The timestamp format is fixed-width, so string order matches time order.
- **Immutability.** File keys are content hashes, and uploads skip objects the uploader believes already exist, based on a local S3 check cache or an S3 `HeadObject` call. Manifest keys include the session action ID, so every manifest key is unique and is never overwritten.
- **Additive outputs.** The output snapshot includes new and modified files and drops deleted ones (`_manifest_snapshot`).
- **No ordering between independent tasks.** How ties are resolved:
  - **Within a step:** output manifests are merged in order of their S3 `LastModified` timestamp, oldest first (`_merge_asset_manifests_sorted_asc_by_last_modified`). `LastModified` reflects when the manifest was written during the output upload, not when the task finished. It has one-second resolution, and ties keep the order of keys in the S3 listing.
  - **Across several dependency steps:** outputs are merged in the order the service returns the step's dependencies.
- **Reader contract.** The worker agent only reads the outputs of dependency steps after the scheduler has released the dependent step.

### Write order

For each asset root, the manifest is written before the files it references (`S3AssetUploader.upload_assets`). This applies to input uploads at submission and to output uploads on the worker. When a task has several asset roots, they're uploaded one after another. Readers that follow the reader contract never observe this, because a run isn't reported as succeeded until all of its files are uploaded.

## Known gaps

- **An interrupted output upload leaves a manifest behind.** This breaks *only successful runs contribute* and *reruns supersede*.
  - **How it happens:** the output upload fails, is canceled, or the worker dies after at least one manifest has been written. This includes a task with several asset roots where a later root fails.
  - **Effect:** the task run doesn't succeed, but its manifest stays in S3 with the newest key. Readers that list S3 pick it as the latest run, including downstream workers.
- **Clock skew between workers.** This breaks *reruns supersede* for readers that list S3. The timestamp in the output manifest key comes from the worker's clock, so two runs of the same task on different workers can be ordered wrongly if the clock difference is bigger than the gap between the runs. Task-level output download isn't affected, because it selects the run by `latestSessionActionId` from the service.
- **Chunked steps don't select a latest run.** This breaks *reruns supersede*.
  - **Merging every run:** steps that use task chunking have no per-task folder, so readers merge every run's manifests in the step. Files written only by an earlier run survive.
  - **Matching by session action ID:** a session action ID has the form `sessionaction-<hex>-<n>`. The hex part is shared by every action in a session, and `n` counts up by one per action. Looking up outputs by session action ID matches the ID as a substring with no end delimiter, and for chunked steps it searches the whole step prefix. In a session that runs more than ten actions, `sessionaction-<hex>-1` also matches `-10` through `-19`, `-100`, and so on, which pulls in the outputs of other chunks from the same session.
- **Dependency outputs are copied next to inputs in copied mode.** This breaks *layering*.
  - **How it happens:** in the worker agent, job inputs and dependency outputs are downloaded in separate actions into the same local root. The default conflict resolution is `CREATE_COPY`.
  - **Effect:** if a dependency step modifies an input file, the downstream task sees the original at the original path and the dependency's version at a renamed path such as `scene (1).exr`.
  - **Not affected:** the virtual filesystem mode merges input and dependency manifests into one before mounting.
- **Reported manifests can go stale within a session.** This breaks *only successful runs contribute* for readers that use the manifests reported to the service, such as incremental output download. The worker agent remembers the last uploaded manifest list for the whole session and doesn't clear it. A later task in the same session that produces no outputs reports `SUCCEEDED` with the previous task's manifests.
- **Output download doesn't follow the reader contract.** This breaks the *reader contract*.
  - **Step-level and job-level download:** these list S3 the same way workers do, so the gaps above apply even after the job finishes.
  - **Task-level download:** this uses `latestSessionActionId` without checking the run's status. If the latest run failed in its task script, it returns nothing, although an older successful run exists. If the latest run is still uploading, it can find a manifest before its files exist.
- **Skipped uploads trust a local cache.** This can break *inputs before start*. The uploader skips files that its local S3 check cache records as uploaded, and it only samples the cache against S3. If a bucket lifecycle rule has expired objects under `Data/`, a new job can reference files that no longer exist.

A related behaviour that's consistent with the invariants but may surprise users: when a rerun of a task fails in the task script, it uploads nothing. The task shows `FAILED`, but readers still see the outputs of the older successful run.
