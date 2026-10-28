# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests that uploads and the S3 check cache honor S3 Lifecycle expiration rules and archive
storage classes, using the metadata that HeadObject returns.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import deadline
from deadline.job_attachments._s3_object_lifetime import OBJECT_EXPIRY_SAFETY_MARGIN
from deadline.job_attachments.asset_manifests import BaseManifestPath, HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03 import AssetManifest
from deadline.job_attachments.caches import S3CheckCache, S3CheckCacheEntry
from deadline.job_attachments.exceptions import JobAttachmentsS3ClientError
from deadline.job_attachments.upload import S3AssetUploader

BUCKET = "test-bucket"
CAS_PREFIX = "root/Data"
FILE = BaseManifestPath(path="test-file.txt", hash="test-hash", size=5, mtime=1)
KEY = f"{CAS_PREFIX}/test-hash.xxh128"
CACHE_KEY = f"{BUCKET}/{KEY}"
LAST_SEEN = 1_800_000_000.0
THIRTY_DAYS = timedelta(days=S3CheckCache.ENTRY_EXPIRY_DAYS).total_seconds()

HeadResult = Union[Dict[str, Any], Exception]


def _not_found() -> ClientError:
    return ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")


def _expiration_in(delta: timedelta) -> Dict[str, Any]:
    expiry = (datetime.now(timezone.utc) + delta).replace(microsecond=0)
    return {
        "Expiration": f'expiry-date="{format_datetime(expiry, usegmt=True)}", rule-id="expire-cas"'
    }


def _expiry_timestamp(head: Dict[str, Any]) -> float:
    date = head["Expiration"].split('"')[1]
    return parsedate_to_datetime(date).timestamp()


def _make_uploader(head_results: List[HeadResult]) -> S3AssetUploader:
    uploader = S3AssetUploader(s3_max_pool_connections=50, small_file_threshold_multiplier=20)
    uploader._s3 = MagicMock()
    uploader._s3.head_object.side_effect = head_results
    return uploader


def _upload_object_to_cas(
    uploader: S3AssetUploader,
    s3_cache: Any,
    tmp_path: Path,
    force_s3_check: Optional[bool] = None,
):
    with patch.object(
        uploader, "_get_current_timestamp", return_value=str(LAST_SEEN)
    ), patch.object(uploader, "upload_file_to_s3") as mock_upload:
        is_uploaded, size = uploader.upload_object_to_cas(
            file=FILE,
            hash_algorithm=HashAlgorithm.XXH128,
            s3_bucket=BUCKET,
            source_root=tmp_path,
            s3_cas_prefix=CAS_PREFIX,
            s3_check_cache=s3_cache,
            force_s3_check=force_s3_check,
        )
    assert size == FILE.size
    assert is_uploaded == mock_upload.called
    return is_uploaded


@pytest.fixture
def empty_cache() -> MagicMock:
    s3_cache = MagicMock()
    s3_cache.get_connection_entry.return_value = None
    return s3_cache


class TestFileAlreadyUploaded:
    @pytest.mark.parametrize(
        "head, expected",
        [
            pytest.param({}, True, id="standard"),
            pytest.param({"StorageClass": "GLACIER_IR"}, True, id="glacier-instant-retrieval"),
            pytest.param({"StorageClass": "GLACIER"}, False, id="glacier"),
            pytest.param({"StorageClass": "DEEP_ARCHIVE"}, False, id="deep-archive"),
            pytest.param(
                {"StorageClass": "INTELLIGENT_TIERING", "ArchiveStatus": "ARCHIVE_ACCESS"},
                False,
                id="intelligent-tiering-archive-access",
            ),
            pytest.param(
                {"StorageClass": "INTELLIGENT_TIERING", "ArchiveStatus": "DEEP_ARCHIVE_ACCESS"},
                False,
                id="intelligent-tiering-deep-archive-access",
            ),
            pytest.param(
                {"StorageClass": "GLACIER", "Restore": 'ongoing-request="true"'},
                False,
                id="glacier-restore-in-progress",
            ),
            pytest.param(
                {"StorageClass": "GLACIER", "Restore": 'ongoing-request="false"'},
                True,
                id="glacier-restored",
            ),
            pytest.param(_expiration_in(timedelta(days=10)), True, id="expires-later"),
            pytest.param(_expiration_in(timedelta(hours=1)), False, id="expires-within-margin"),
            pytest.param(_expiration_in(-timedelta(days=1)), False, id="already-expired"),
        ],
    )
    def test_file_already_uploaded(self, head: Dict[str, Any], expected: bool):
        uploader = _make_uploader([head])
        assert uploader.file_already_uploaded(BUCKET, KEY) is expected
        uploader._s3.head_object.assert_called_once_with(Bucket=BUCKET, Key=KEY)

    def test_restored_archive_whose_restore_expires_soon_is_not_uploaded(self):
        restore_expiry = datetime.now(timezone.utc) + timedelta(hours=2)
        uploader = _make_uploader(
            [
                {
                    "StorageClass": "DEEP_ARCHIVE",
                    "Restore": f'ongoing-request="false", expiry-date="{format_datetime(restore_expiry, usegmt=True)}"',
                }
            ]
        )
        assert not uploader.file_already_uploaded(BUCKET, KEY)

    def test_not_found(self):
        uploader = _make_uploader([_not_found()])
        assert not uploader.file_already_uploaded(BUCKET, KEY)

    def test_access_denied_still_raises(self):
        uploader = _make_uploader(
            [ClientError({"ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject")]
        )
        with patch(
            f"{deadline.__package__}.job_attachments.upload.get_account_id", return_value=None
        ), pytest.raises(JobAttachmentsS3ClientError):
            uploader.file_already_uploaded(BUCKET, KEY)


class TestUploadObjectToCas:
    def test_existing_object_without_expiration_is_cached_for_entry_expiry_days(
        self, tmp_path, empty_cache
    ):
        uploader = _make_uploader([{}])

        assert not _upload_object_to_cas(uploader, empty_cache, tmp_path)

        uploader._s3.head_object.assert_called_once()
        empty_cache.put_entry.assert_called_once_with(
            S3CheckCacheEntry(CACHE_KEY, str(LAST_SEEN), str(LAST_SEEN + THIRTY_DAYS))
        )

    def test_existing_object_with_expiration_is_cached_until_safety_margin_before_it(
        self, tmp_path, empty_cache
    ):
        head = _expiration_in(timedelta(days=10))
        uploader = _make_uploader([head])

        assert not _upload_object_to_cas(uploader, empty_cache, tmp_path)

        uploader._s3.head_object.assert_called_once()
        empty_cache.put_entry.assert_called_once_with(
            S3CheckCacheEntry(
                CACHE_KEY,
                str(LAST_SEEN),
                str(_expiry_timestamp(head) - OBJECT_EXPIRY_SAFETY_MARGIN.total_seconds()),
            )
        )

    def test_existing_object_with_expiration_beyond_cap_is_cached_for_entry_expiry_days(
        self, tmp_path, empty_cache
    ):
        # LAST_SEEN is fixed in the future relative to the real clock, so an expiration well past
        # LAST_SEEN + 30 days must be capped.
        head = _expiration_in(
            timedelta(seconds=LAST_SEEN - datetime.now().timestamp()) + timedelta(days=90)
        )
        uploader = _make_uploader([head])

        assert not _upload_object_to_cas(uploader, empty_cache, tmp_path)

        empty_cache.put_entry.assert_called_once_with(
            S3CheckCacheEntry(CACHE_KEY, str(LAST_SEEN), str(LAST_SEEN + THIRTY_DAYS))
        )

    @pytest.mark.parametrize(
        "head",
        [
            pytest.param({"StorageClass": "GLACIER"}, id="glacier"),
            pytest.param({"StorageClass": "DEEP_ARCHIVE"}, id="deep-archive"),
            pytest.param(
                {"StorageClass": "INTELLIGENT_TIERING", "ArchiveStatus": "ARCHIVE_ACCESS"},
                id="intelligent-tiering-archive-access",
            ),
            pytest.param(_expiration_in(timedelta(hours=1)), id="expires-within-margin"),
        ],
    )
    @pytest.mark.parametrize("force_s3_check", [None, False, True])
    def test_unusable_existing_object_is_uploaded_again(
        self, tmp_path, empty_cache, head, force_s3_check
    ):
        # After re-uploading, the object is in the default storage class with no expiration.
        uploader = _make_uploader([head, {}])

        assert _upload_object_to_cas(uploader, empty_cache, tmp_path, force_s3_check)

        assert uploader._s3.head_object.call_count == 2
        empty_cache.put_entry.assert_called_once_with(
            S3CheckCacheEntry(CACHE_KEY, str(LAST_SEEN), str(LAST_SEEN + THIRTY_DAYS))
        )

    def test_new_object_cache_entry_honors_expiration_returned_after_upload(
        self, tmp_path, empty_cache
    ):
        after_upload = _expiration_in(timedelta(days=7))
        uploader = _make_uploader([_not_found(), after_upload])

        assert _upload_object_to_cas(uploader, empty_cache, tmp_path)

        assert uploader._s3.head_object.call_count == 2
        entry = empty_cache.put_entry.call_args.args[0]
        assert entry.expiry_time == str(
            _expiry_timestamp(after_upload) - OBJECT_EXPIRY_SAFETY_MARGIN.total_seconds()
        )

    def test_new_object_is_cached_for_entry_expiry_days_if_check_after_upload_fails(
        self, tmp_path, empty_cache
    ):
        uploader = _make_uploader([_not_found(), RuntimeError("network blip")])

        assert _upload_object_to_cas(uploader, empty_cache, tmp_path)

        empty_cache.put_entry.assert_called_once_with(
            S3CheckCacheEntry(CACHE_KEY, str(LAST_SEEN), str(LAST_SEEN + THIRTY_DAYS))
        )

    def test_new_object_is_not_cached_if_missing_after_upload(self, tmp_path, empty_cache):
        # For example, upload_file_to_s3 skips files that are symlinks or no longer exist.
        uploader = _make_uploader([_not_found(), _not_found()])

        assert _upload_object_to_cas(uploader, empty_cache, tmp_path)

        empty_cache.put_entry.assert_not_called()

    def test_new_object_is_not_cached_if_it_expires_within_margin(self, tmp_path, empty_cache):
        uploader = _make_uploader([_not_found(), _expiration_in(timedelta(hours=1))])

        assert _upload_object_to_cas(uploader, empty_cache, tmp_path)

        empty_cache.put_entry.assert_not_called()

    def test_cache_hit_skips_head_and_upload(self, tmp_path):
        s3_cache = MagicMock()
        s3_cache.get_connection_entry.return_value = S3CheckCacheEntry(CACHE_KEY, str(LAST_SEEN))
        uploader = _make_uploader([])

        assert not _upload_object_to_cas(uploader, s3_cache, tmp_path)

        uploader._s3.head_object.assert_not_called()
        s3_cache.put_entry.assert_not_called()


class TestUploadWithRealCache:
    """End-to-end through a real S3CheckCache, to check what a later submission sees."""

    def _upload(self, uploader: S3AssetUploader, cache_dir: Path, tmp_path: Path) -> bool:
        with S3CheckCache(str(cache_dir)) as s3_cache, patch.object(
            uploader, "upload_file_to_s3"
        ) as mock_upload:
            uploader.upload_object_to_cas(
                file=FILE,
                hash_algorithm=HashAlgorithm.XXH128,
                s3_bucket=BUCKET,
                source_root=tmp_path,
                s3_cas_prefix=CAS_PREFIX,
                s3_check_cache=s3_cache,
            )
            return mock_upload.called

    def test_entry_is_trusted_until_lifecycle_expiry_safety_margin(self, tmp_path):
        cache_dir = tmp_path / "cache"
        head = _expiration_in(timedelta(days=3))
        uploader = _make_uploader([head])

        assert not self._upload(uploader, cache_dir, tmp_path)

        expected_expiry = _expiry_timestamp(head) - OBJECT_EXPIRY_SAFETY_MARGIN.total_seconds()
        with S3CheckCache(str(cache_dir)) as s3_cache:
            entry = s3_cache.get_entry(CACHE_KEY)
            assert entry is not None
            assert entry.expiry_time is not None
            assert float(entry.expiry_time) == pytest.approx(expected_expiry)

            # Once the safety margin before the lifecycle expiry is reached, the entry is a miss.
            later = datetime.fromtimestamp(expected_expiry + 1)
            with patch(
                f"{deadline.__package__}.job_attachments.caches.s3_check_cache.datetime"
            ) as dt:
                dt.now.return_value = later
                dt.fromtimestamp.side_effect = datetime.fromtimestamp
                assert s3_cache.get_entry(CACHE_KEY) is None

        # Before then, a second submission trusts the cache and makes no S3 calls.
        assert not self._upload(uploader, cache_dir, tmp_path)
        uploader._s3.head_object.assert_called_once()

    def test_archived_object_is_not_cached(self, tmp_path):
        cache_dir = tmp_path / "cache"
        # The archived object is re-uploaded, and the HEAD after the upload fails.
        uploader = _make_uploader([{"StorageClass": "DEEP_ARCHIVE"}, _not_found()])

        assert self._upload(uploader, cache_dir, tmp_path)

        with S3CheckCache(str(cache_dir)) as s3_cache:
            assert s3_cache.get_entry(CACHE_KEY) is None


class TestVerifyHashCacheIntegrity:
    @pytest.mark.parametrize(
        "head, expected",
        [
            pytest.param({}, True, id="present"),
            pytest.param({"StorageClass": "GLACIER"}, False, id="archived"),
            pytest.param(_expiration_in(timedelta(hours=1)), False, id="expires-within-margin"),
            pytest.param(_not_found(), False, id="missing"),
        ],
    )
    def test_sampled_entries_must_be_usable(self, head: HeadResult, expected: bool):
        s3_cache_impl = MagicMock()
        s3_cache_impl.get_connection_entry.return_value = S3CheckCacheEntry(
            CACHE_KEY, str(datetime.now().timestamp())
        )
        s3_cache = MagicMock()
        s3_cache.__enter__.return_value = s3_cache_impl
        uploader = _make_uploader([head])

        with patch(
            f"{deadline.__package__}.job_attachments.upload.S3CheckCache",
            return_value=s3_cache,
        ):
            assert (
                uploader.verify_hash_cache_integrity(
                    s3_check_cache_dir="cache-dir",
                    manifest=AssetManifest(
                        hash_alg=HashAlgorithm.XXH128, paths=[FILE], total_size=FILE.size
                    ),
                    s3_cas_prefix=CAS_PREFIX,
                    s3_bucket=BUCKET,
                )
                is expected
            )
