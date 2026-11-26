# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for how _download_all_manifests_with_absolute_paths handles output manifests that
S3 reports as not found (404) or forbidden (403).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from deadline.job_attachments._incremental_downloads._manifest_s3_downloads import (
    _download_all_manifests_with_absolute_paths,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03 import AssetManifest, ManifestPath
from deadline.job_attachments.exceptions import JobAttachmentsS3ClientError

MODULE = "deadline.job_attachments._incremental_downloads._manifest_s3_downloads"
BUCKET = "test-bucket"
ROOT_PREFIX = "test-prefix"
JOB_IDS = [f"job-{c * 32}" for c in "abc"]


def _session_action_id(job_id: str) -> str:
    return f"sessionaction-{job_id[4:]}-0"


def _make_inputs(tmp_path: Path) -> tuple[dict, dict, dict]:
    """Builds a queue, and three jobs that each have one session action with one output manifest."""
    queue = {"jobAttachmentSettings": {"s3BucketName": BUCKET, "rootPrefix": ROOT_PREFIX}}
    jobs: dict[str, dict[str, Any]] = {}
    job_sessions: dict[str, list] = {}
    for job_id in JOB_IDS:
        jobs[job_id] = {
            "jobId": job_id,
            "attachments": {"manifests": [{"rootPath": str(tmp_path / job_id)}]},
        }
        session_action_id = _session_action_id(job_id)
        job_sessions[job_id] = [
            {
                "sessionActions": [
                    {
                        "sessionActionId": session_action_id,
                        "manifests": [
                            {
                                "outputManifestPath": f"farm-1/queue-1/{job_id}/step-1/task-1-0/"
                                f"{session_action_id}/manifest_output"
                            }
                        ],
                    }
                ]
            }
        ]
    return queue, jobs, job_sessions


def _fake_manifest_download(status_codes: dict[str, int]):
    """
    Returns a fake for _get_asset_root_and_manifest_from_s3_with_last_modified that raises
    JobAttachmentsS3ClientError with the given status code for the job ids in status_codes.
    """

    def fake(manifest_key: str, s3_bucket: str, session: Optional[Any] = None):
        for job_id, status_code in status_codes.items():
            if job_id in manifest_key:
                raise JobAttachmentsS3ClientError(
                    action="downloading binary file",
                    status_code=status_code,
                    bucket_name=s3_bucket,
                    key_or_prefix=manifest_key,
                )
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestPath(path="out.txt", hash="a", size=1, mtime=int(time.time() * 1e6))],
            total_size=1,
        )
        return None, datetime.now(tz=timezone.utc), manifest

    return fake


def _run(
    tmp_path: Path,
    status_codes: dict[str, int],
    output_missing_manifest_job_ids: Optional[set[str]],
) -> list:
    queue, jobs, job_sessions = _make_inputs(tmp_path)
    with patch(
        f"{MODULE}._get_asset_root_and_manifest_from_s3_with_last_modified",
        side_effect=_fake_manifest_download(status_codes),
    ):
        return _download_all_manifests_with_absolute_paths(
            queue,
            jobs,
            job_sessions,
            {},
            {},
            boto3_session_for_s3=None,  # type: ignore[arg-type]
            output_missing_manifest_job_ids=output_missing_manifest_job_ids,
        )


def _root_paths(downloaded_manifests: list) -> set[str]:
    return {str(Path(manifest.paths[0].path).parent) for _, manifest in downloaded_manifests}


@pytest.mark.parametrize(
    ("status_code", "expected_reason"),
    [
        (404, "output manifest not found for job"),
        (403, "output manifest not found or access denied for job"),
    ],
)
def test_one_missing_manifest_is_skipped(tmp_path, caplog, status_code, expected_reason):
    # GIVEN one job whose output manifest returns a 404 or 403, and two that download
    missing_job_ids: set[str] = set()

    # WHEN
    with caplog.at_level(logging.WARNING):
        downloaded = _run(tmp_path, {JOB_IDS[1]: status_code}, missing_job_ids)

    # THEN the other two manifests are returned and the job is reported as missing
    assert missing_job_ids == {JOB_IDS[1]}
    assert _root_paths(downloaded) == {str(tmp_path / JOB_IDS[0]), str(tmp_path / JOB_IDS[2])}
    # The warning names the job and session action, but not the bucket or key
    assert expected_reason in caplog.text
    assert JOB_IDS[1] in caplog.text
    assert _session_action_id(JOB_IDS[1]) in caplog.text
    assert BUCKET not in caplog.text
    assert "manifest_output" not in caplog.text


def test_all_manifests_forbidden_raises(tmp_path, caplog):
    # GIVEN every manifest returns 403, which points at a permissions problem
    missing_job_ids: set[str] = set()
    printed: list[str] = []
    queue, jobs, job_sessions = _make_inputs(tmp_path)
    fake = _fake_manifest_download({job_id: 403 for job_id in JOB_IDS})

    def first_manifest_finishes_last(manifest_key: str, *args, **kwargs):
        # Make the first manifest's error arrive last, so the test checks that the raised
        # error is chosen by position in the list and not by completion order.
        if JOB_IDS[0] in manifest_key:
            time.sleep(0.2)
        return fake(manifest_key, *args, **kwargs)

    # WHEN/THEN the 403 error is raised instead of reporting every job as missing
    with (
        caplog.at_level(logging.WARNING),
        patch(
            f"{MODULE}._get_asset_root_and_manifest_from_s3_with_last_modified",
            side_effect=first_manifest_finishes_last,
        ),
        pytest.raises(JobAttachmentsS3ClientError) as exc_info,
    ):
        _download_all_manifests_with_absolute_paths(
            queue,
            jobs,
            job_sessions,
            {},
            {},
            boto3_session_for_s3=None,  # type: ignore[arg-type]
            print_function_callback=printed.append,
            output_missing_manifest_job_ids=missing_job_ids,
        )
    assert exc_info.value.status_code == 403
    # The error raised is the one for the first manifest in the list
    assert JOB_IDS[0] in exc_info.value.key_or_prefix
    assert missing_job_ids == set()
    # Nothing claims a manifest is being skipped, because none is
    assert "Skipping" not in caplog.text
    assert not any("Skipping" in str(line) for line in printed)


def test_all_manifests_fail_with_mixed_403_and_404_are_skipped(tmp_path):
    # GIVEN every manifest fails, but not all with 403
    missing_job_ids: set[str] = set()

    # WHEN
    downloaded = _run(
        tmp_path, {JOB_IDS[0]: 403, JOB_IDS[1]: 404, JOB_IDS[2]: 403}, missing_job_ids
    )

    # THEN they are all skipped rather than raised
    assert downloaded == []
    assert missing_job_ids == set(JOB_IDS)


def test_no_manifests_with_set_returns_empty(tmp_path):
    # GIVEN no jobs have output manifests
    queue, _, _ = _make_inputs(tmp_path)
    missing_job_ids: set[str] = set()

    # WHEN
    downloaded = _download_all_manifests_with_absolute_paths(
        queue,
        {},
        {},
        {},
        {},
        boto3_session_for_s3=None,  # type: ignore[arg-type]
        output_missing_manifest_job_ids=missing_job_ids,
    )

    # THEN nothing is returned or reported, and nothing is raised
    assert downloaded == []
    assert missing_job_ids == set()


def test_all_manifests_not_found_are_skipped(tmp_path):
    # GIVEN every manifest returns 404
    missing_job_ids: set[str] = set()

    # WHEN
    downloaded = _run(tmp_path, {job_id: 404 for job_id in JOB_IDS}, missing_job_ids)

    # THEN nothing is returned and every job is reported as missing
    assert downloaded == []
    assert missing_job_ids == set(JOB_IDS)


def test_other_errors_still_raise(tmp_path):
    # GIVEN one manifest returns a 500
    missing_job_ids: set[str] = set()

    # WHEN/THEN it is raised
    with pytest.raises(JobAttachmentsS3ClientError) as exc_info:
        _run(tmp_path, {JOB_IDS[0]: 500}, missing_job_ids)
    assert exc_info.value.status_code == 500


@pytest.mark.parametrize("status_code", [403, 404])
def test_default_still_raises_on_missing_manifest(tmp_path, status_code):
    # GIVEN the caller doesn't pass output_missing_manifest_job_ids
    # WHEN/THEN a missing manifest is raised, as before
    with pytest.raises(JobAttachmentsS3ClientError) as exc_info:
        _run(tmp_path, {JOB_IDS[0]: status_code}, None)
    assert exc_info.value.status_code == status_code


def test_no_errors_returns_every_manifest(tmp_path):
    missing_job_ids: set[str] = set()

    downloaded = _run(tmp_path, {}, missing_job_ids)

    assert len(downloaded) == len(JOB_IDS)
    assert missing_job_ids == set()
