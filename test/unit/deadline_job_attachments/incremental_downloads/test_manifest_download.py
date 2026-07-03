# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import uuid
import random
import os
import time
from pathlib import Path, PurePosixPath
from datetime import datetime
import sys

import boto3
from moto import mock_aws
import pytest

from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm, hash_data
from deadline.job_attachments.asset_manifests import BaseAssetManifest, BaseManifestPath
from deadline.job_attachments.asset_manifests.v2023_03_03 import (
    AssetManifest,
    ManifestPath,
)
from deadline.job_attachments.progress_tracker import (
    ProgressReportMetadata,
)

from deadline.job_attachments._incremental_downloads._manifest_s3_downloads import (
    _download_all_manifests_with_absolute_paths,
    _merge_absolute_path_manifest_list,
    _download_manifest_paths,
)
from deadline.job_attachments.models import FileConflictResolution

"""
Tests the manifest and file download functionality used by the queue incremental download operation,
by generating fake job data and populating a moto mocked S3 bucket with actual manifests and files.
"""


def generate_random_path():
    """Generate a random path with a few subdirectories."""
    part_count = random.randrange(1, 3)
    return str(
        PurePosixPath(
            *[f"part-{i}-{random.randrange(4)}" for i in range(part_count - 1)],
            f"{uuid.uuid4()}.ext",
        )
    )


def generate_random_files(file_count, file_size_min, file_size_max):
    random_files = {}
    for i in range(file_count):
        file_size = random.randrange(file_size_min, file_size_max)
        file_contents = random.randbytes(file_size)  #  type: ignore
        file_path = generate_random_path()

        random_files[file_path] = file_contents

    return random_files


def generate_fake_job_with_output_manifest(
    tmp_path: Path,
    queue: dict,
    file_count: int,
    file_size_min: int,
    file_size_max: int,
    out_jobs: list,
    out_job_sessions: dict,
    out_expected_download_files: dict,
):
    """
    Given a fake queue and a moto session for s3, generates a fake job with corresponding
    session and session actions. Puts files and manifests into S3, with paths under the tmp_path
    directory. Adds all the files to expected_download_files as {abs_path: file_contents}.
    """
    manifest_count = random.randrange(1, 5)

    s3 = boto3.resource("s3")
    bucket = s3.Bucket(queue["jobAttachmentSettings"]["s3BucketName"])

    # Create the fake job
    job_id = f"job-{str(uuid.uuid4()).replace('-', '')}"

    # Initialize the manifests for the job
    job_manifests: list = [
        {
            "rootPath": str(tmp_path / f"{job_id}-root-path-{i}"),
            "rootPathFormat": "POSIX",
            "outputRelativeDirectories": ["."],
        }
        for i in range(manifest_count)
    ]

    job = {
        "jobId": job_id,
        "name": f"test-job-{job_id}",
        "attachments": {"manifests": job_manifests},
    }
    out_jobs.append(job)

    # Generate random files
    random_files = generate_random_files(file_count, file_size_min, file_size_max)

    # Divide the files randomly among the manifests, and add to the expected download files
    files_in_manifests: list = [{} for i in range(manifest_count)]
    for file, contents in random_files.items():
        manifest_index = random.randrange(manifest_count)
        files_in_manifests[manifest_index][file] = contents
        out_expected_download_files[str(Path(job_manifests[manifest_index]["rootPath"]) / file)] = (
            contents
        )

    # Put the files and manifests into S3, and record their locations
    session_action_manifests = []
    for manifest_index, files in enumerate(files_in_manifests):
        total_size = 0
        paths: list[BaseManifestPath] = []
        for file, contents in files.items():
            s3_key = f"{queue['jobAttachmentSettings']['rootPrefix']}/Data/{hash_data(contents, HashAlgorithm.XXH128)}.xxh128"
            bucket.put_object(
                Key=s3_key,
                Body=contents,
            )
            paths.append(
                ManifestPath(
                    path=file,
                    hash=hash_data(contents, HashAlgorithm.XXH128),
                    size=len(contents),
                    mtime=int(time.time() * 1e6),
                )
            )
            total_size += len(contents)

        if paths:
            manifest = AssetManifest(
                hash_alg=HashAlgorithm.XXH128, paths=paths, total_size=total_size
            )
            manifest_bytes = manifest.encode().encode("utf-8")
            manifest_hash = hash_data(manifest_bytes, HashAlgorithm.XXH128)
            bucket.put_object(
                Key=f"{queue['jobAttachmentSettings']['rootPrefix']}/Manifests/{manifest_hash}.xxh128",
                Body=manifest_bytes,
            )

            session_action_manifests.append(
                {
                    "outputManifestHash": manifest_hash,
                    "outputManifestPath": f"{manifest_hash}.xxh128",
                }
            )
        else:
            session_action_manifests.append({})

    # Use one session for the job
    session: dict = {
        "sessionId": f"session-{str(uuid.uuid4()).replace('-', '')}",
        "fleetId": f"fleet-{str(uuid.uuid4()).replace('-', '')}",
        "workerId": f"worker-{str(uuid.uuid4()).replace('-', '')}",
        "startedAt": "2025-08-06T00:15:45.712000+00:00",
        "endedAt": "2025-08-06T00:20:59.992000+00:00",
        "lifecycleStatus": "ENDED",
    }
    out_job_sessions[job_id] = [session]

    # Use one session action in the session
    session_action: dict = {
        "sessionActionId": session["sessionId"].replace("session-", "sessionaction-") + "-0",
        "status": "SUCCEEDED",
        "startedAt": "2025-08-06T00:20:58.454000+00:00",
        "endedAt": "2025-08-06T00:20:59.992000+00:00",
        "progressPercent": 100.0,
        "definition": {
            "taskRun": {
                "taskId": "task-b1764261dff54214aace3932bde8ae7e-0",
                "stepId": "step-b1764261dff54214aace3932bde8ae7e",
            }
        },
        # This test doesn't go into the S3 object layer, so the manifests list is empty.
        "manifests": session_action_manifests,
    }
    session["sessionActions"] = [session_action]


@pytest.mark.skipif(
    sys.version_info < (3, 9), reason="test uses random.randbytes which is Python >= 3.9"
)
@mock_aws
def test_manifest_and_output_downloads(tmp_path):
    """
    This test uses moto3 to mock a bunch of job attachment output data in S3, and then
    calls the sequence of functions used in incremental downloads
    """
    queue_id = "queue-01234567890123456789012345678901"
    bucket_name = "test-bucket"
    root_prefix = "test-prefix"

    # Create S3 client and bucket
    boto3_session = boto3.Session(region_name="us-west-2")
    s3_client = boto3_session.client("s3", region_name="us-west-2")
    s3_client.create_bucket(
        Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )

    # Create queue structure
    queue = {
        "queueId": queue_id,
        "jobAttachmentSettings": {"s3BucketName": bucket_name, "rootPrefix": root_prefix},
    }

    jobs: list = []
    job_sessions: dict = {}
    expected_download_files: dict = {}
    # Generate fake jobs, mostly with small files and one job with files > 1MB so that the test runs both
    # _download_file_with_get_object and _download_file_with_transfer_manager
    generate_fake_job_with_output_manifest(
        tmp_path, queue, 2, 2, 1024, jobs, job_sessions, expected_download_files
    )
    generate_fake_job_with_output_manifest(
        tmp_path, queue, 30, 2, 50, jobs, job_sessions, expected_download_files
    )
    generate_fake_job_with_output_manifest(
        tmp_path, queue, 2, 1500000, 2000000, jobs, job_sessions, expected_download_files
    )

    # WHEN: Download all the output manifests for all the jobs we made, and make their paths absolute
    unmapped_paths: dict = {}
    downloaded_manifests: list[tuple[datetime, BaseAssetManifest]] = (
        _download_all_manifests_with_absolute_paths(
            queue,
            {job["jobId"]: job for job in jobs},
            job_sessions,
            {},
            unmapped_paths,
            boto3_session,
            print,
        )
    )

    # THEN: There should be no unmapped paths because we provided {} for the path mapping applier
    assert unmapped_paths == {}
    # All the manifest paths should be absolute
    for _, manifest in downloaded_manifests:
        for manifest_path in manifest.paths:
            assert os.path.isabs(manifest_path.path)

    # WHEN: Merge all the manifests into one list of paths
    manifest_paths_to_download: list[BaseManifestPath] = _merge_absolute_path_manifest_list(
        downloaded_manifests
    )

    # THEN: The full set of paths should exactly match the keys of expected_download_files
    assert {v.path for v in manifest_paths_to_download} == set(expected_download_files.keys())

    # WHEN: Download all the paths from the manifests
    def on_downloading_files(
        download_metadata: ProgressReportMetadata,
    ) -> bool:
        return True

    _download_manifest_paths(
        manifest_paths_to_download,
        HashAlgorithm.XXH128,
        queue,
        boto3_session,
        FileConflictResolution.OVERWRITE,
        on_downloading_files=on_downloading_files,
        print_function_callback=print,
    )

    # THEN: All the files should be downloaded, and match the randomly generated contents
    for file, contents in expected_download_files.items():
        assert os.path.exists(file)
        assert os.path.isfile(file)
        with open(file, "rb") as fh:
            assert fh.read() == contents


def _put_manifest_in_s3(queue: dict, paths: list[BaseManifestPath]) -> str:
    """
    Helper that builds an AssetManifest from the given paths, stores it in the moto S3 bucket,
    and returns the S3 key under the queue's Manifests prefix.
    """
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(queue["jobAttachmentSettings"]["s3BucketName"])
    total_size = sum(p.size for p in paths)
    manifest = AssetManifest(hash_alg=HashAlgorithm.XXH128, paths=paths, total_size=total_size)
    manifest_bytes = manifest.encode().encode("utf-8")
    manifest_hash = hash_data(manifest_bytes, HashAlgorithm.XXH128)
    key = f"{queue['jobAttachmentSettings']['rootPrefix']}/Manifests/{manifest_hash}.xxh128"
    bucket.put_object(Key=key, Body=manifest_bytes)
    return key


@mock_aws
def test_traversal_paths_are_dropped_without_path_mapping(tmp_path):
    """
    Regression test for the path traversal vulnerability where a malicious manifest with
    absolute paths or ".." segments could write files outside of the root path during an
    incremental download. With no path mapping applier (same storage profile or
    --ignore-storage-profiles), traversal paths must be dropped into unmapped_paths rather
    than written to the local workstation.
    """
    from deadline.job_attachments._incremental_downloads._manifest_s3_downloads import (
        _download_manifest_and_make_paths_absolute,
    )

    bucket_name = "test-bucket"
    root_prefix = "test-prefix"
    boto3_session = boto3.Session(region_name="us-west-2")
    s3_client = boto3_session.client("s3", region_name="us-west-2")
    s3_client.create_bucket(
        Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )

    queue = {
        "queueId": "queue-01234567890123456789012345678901",
        "jobAttachmentSettings": {"s3BucketName": bucket_name, "rootPrefix": root_prefix},
    }

    root_path = str(tmp_path / "download-root")

    # A safe file plus malicious paths: a relative traversal and an absolute path.
    safe_path = ManifestPath(path="subdir/safe.txt", hash="a" * 32, size=1, mtime=1)
    traversal_path = ManifestPath(
        path="../../../../../../../../tmp/evil.txt", hash="b" * 32, size=1, mtime=1
    )
    absolute_path = ManifestPath(path="/etc/cron.d/evil", hash="c" * 32, size=1, mtime=1)

    manifest_key = _put_manifest_in_s3(queue, [safe_path, traversal_path, absolute_path])

    output_manifests: list = [None]
    output_unmapped_paths: list = []

    _download_manifest_and_make_paths_absolute(
        index=0,
        queue=queue,
        job_id="job-1",
        root_path=root_path,
        manifest_s3_key=manifest_key,
        path_mapping_rule_applier=None,
        boto3_session_for_s3=boto3_session,
        output_manifests=output_manifests,
        output_unmapped_paths=output_unmapped_paths,
    )

    _, manifest = output_manifests[0]

    # The safe path should survive and be made absolute under the root path.
    surviving_paths = {mp.path for mp in manifest.paths}
    expected_safe = os.path.normpath(os.path.join(root_path, "subdir/safe.txt"))
    assert surviving_paths == {expected_safe}

    # Every surviving path must be contained within the root path.
    for mp in manifest.paths:
        assert os.path.commonpath([os.path.normpath(root_path), mp.path]) == os.path.normpath(
            root_path
        )

    # Both the traversal and the absolute path must be dropped (reported as unmapped),
    # not written. The recorded path is the normalized join result, so assert on count
    # and that none of the dropped paths remain within the root.
    assert len(output_unmapped_paths) == 2
    for dropped_job_id, dropped_path in output_unmapped_paths:
        assert dropped_job_id == "job-1"
        assert os.path.commonpath(
            [os.path.normpath(root_path), os.path.normpath(dropped_path)]
        ) != os.path.normpath(root_path)


@mock_aws
def test_absolute_path_within_root_is_allowed(tmp_path):
    """
    A manifest path that, after joining/normalizing, still resolves within the root path
    must be preserved.
    """
    from deadline.job_attachments._incremental_downloads._manifest_s3_downloads import (
        _download_manifest_and_make_paths_absolute,
    )

    bucket_name = "test-bucket"
    root_prefix = "test-prefix"
    boto3_session = boto3.Session(region_name="us-west-2")
    s3_client = boto3_session.client("s3", region_name="us-west-2")
    s3_client.create_bucket(
        Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )

    queue = {
        "queueId": "queue-01234567890123456789012345678901",
        "jobAttachmentSettings": {"s3BucketName": bucket_name, "rootPrefix": root_prefix},
    }

    root_path = str(tmp_path / "download-root")

    # A path with internal ".." segments that still stays under the root.
    nested_path = ManifestPath(path="a/b/../c/file.txt", hash="d" * 32, size=1, mtime=1)
    manifest_key = _put_manifest_in_s3(queue, [nested_path])

    output_manifests: list = [None]
    output_unmapped_paths: list = []

    _download_manifest_and_make_paths_absolute(
        index=0,
        queue=queue,
        job_id="job-1",
        root_path=root_path,
        manifest_s3_key=manifest_key,
        path_mapping_rule_applier=None,
        boto3_session_for_s3=boto3_session,
        output_manifests=output_manifests,
        output_unmapped_paths=output_unmapped_paths,
    )

    _, manifest = output_manifests[0]
    expected = os.path.normpath(os.path.join(root_path, "a/b/../c/file.txt"))
    assert {mp.path for mp in manifest.paths} == {expected}
    assert output_unmapped_paths == []
