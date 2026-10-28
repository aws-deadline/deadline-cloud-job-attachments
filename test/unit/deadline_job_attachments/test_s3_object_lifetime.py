# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for interpreting S3 HeadObject lifecycle metadata."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

import pytest

from deadline.job_attachments._s3_object_lifetime import (
    OBJECT_EXPIRY_SAFETY_MARGIN,
    S3ObjectLifetime,
    _is_restore_completed,
    _parse_expiry_date,
    get_object_lifetime,
)

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
SOON = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 12, 25, 0, 0, 0, tzinfo=timezone.utc)


def _http_date(dt: datetime) -> str:
    return format_datetime(dt, usegmt=True)


def _expiration(dt: datetime, rule_id: str = "expire-cas") -> str:
    return f'expiry-date="{_http_date(dt)}", rule-id="{rule_id}"'


def _completed_restore(dt: datetime) -> str:
    return f'ongoing-request="false", expiry-date="{_http_date(dt)}"'


class TestParseExpiryDate:
    def test_parses_expiration_header(self):
        assert _parse_expiry_date(_expiration(SOON)) == SOON

    def test_parses_restore_header(self):
        assert _parse_expiry_date(_completed_restore(LATER)) == LATER

    def test_parses_documented_example(self):
        # From the S3 HeadObject API reference.
        value = 'expiry-date="Fri, 23 Dec 2012 00:00:00 GMT", rule-id="picture-deletion-rule"'
        assert _parse_expiry_date(value) == datetime(2012, 12, 23, tzinfo=timezone.utc)

    def test_result_is_timezone_aware(self):
        parsed = _parse_expiry_date(_expiration(SOON))
        assert parsed is not None and parsed.tzinfo is not None

    def test_unknown_zone_is_treated_as_utc(self):
        value = 'expiry-date="Fri, 23 Dec 2012 00:00:00 -0000", rule-id="r"'
        assert _parse_expiry_date(value) == datetime(2012, 12, 23, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="missing"),
            pytest.param("", id="empty"),
            pytest.param('rule-id="no-date"', id="no-expiry-date-field"),
            pytest.param('ongoing-request="true"', id="ongoing-restore"),
            pytest.param('expiry-date="not a date", rule-id="r"', id="malformed-date"),
            pytest.param('expiry-date="", rule-id="r"', id="empty-date"),
            pytest.param(12345, id="not-a-string"),
            pytest.param(MagicMock(), id="mock"),
        ],
    )
    def test_returns_none_for_unusable_values(self, value):
        assert _parse_expiry_date(value) is None


class TestIsRestoreCompleted:
    @pytest.mark.parametrize(
        "value, expected",
        [
            pytest.param(_completed_restore(LATER), True, id="completed-with-expiry"),
            pytest.param('ongoing-request="false"', True, id="completed-without-expiry"),
            pytest.param('ongoing-request="FALSE"', True, id="case-insensitive"),
            pytest.param('ongoing-request="true"', False, id="in-progress"),
            pytest.param(None, False, id="missing"),
            pytest.param("", False, id="empty"),
            pytest.param("garbage", False, id="malformed"),
            pytest.param(MagicMock(), False, id="mock"),
        ],
    )
    def test_is_restore_completed(self, value, expected):
        assert _is_restore_completed(value) is expected


class TestGetObjectLifetime:
    @pytest.mark.parametrize(
        "response",
        [
            pytest.param({}, id="empty-response"),
            pytest.param({"StorageClass": "STANDARD"}, id="standard"),
            pytest.param({"StorageClass": "STANDARD_IA"}, id="standard-ia"),
            pytest.param({"StorageClass": "ONEZONE_IA"}, id="onezone-ia"),
            pytest.param({"StorageClass": "GLACIER_IR"}, id="glacier-instant-retrieval"),
            pytest.param({"StorageClass": "INTELLIGENT_TIERING"}, id="intelligent-tiering"),
        ],
    )
    def test_readable_storage_classes(self, response):
        assert get_object_lifetime(response) == S3ObjectLifetime(readable=True, readable_until=None)

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param({"StorageClass": "GLACIER"}, id="glacier-flexible-retrieval"),
            pytest.param({"StorageClass": "DEEP_ARCHIVE"}, id="deep-archive"),
            pytest.param(
                {"StorageClass": "INTELLIGENT_TIERING", "ArchiveStatus": "ARCHIVE_ACCESS"},
                id="intelligent-tiering-archive-access",
            ),
            pytest.param(
                {"StorageClass": "INTELLIGENT_TIERING", "ArchiveStatus": "DEEP_ARCHIVE_ACCESS"},
                id="intelligent-tiering-deep-archive-access",
            ),
            pytest.param(
                {"StorageClass": "GLACIER", "Restore": 'ongoing-request="true"'},
                id="glacier-restore-in-progress",
            ),
            pytest.param(
                {"StorageClass": "DEEP_ARCHIVE", "Restore": "garbage"},
                id="deep-archive-malformed-restore",
            ),
            pytest.param(
                {"StorageClass": "GLACIER", "Expiration": _expiration(LATER)},
                id="glacier-with-expiration",
            ),
        ],
    )
    def test_archived_objects_are_not_readable(self, response):
        lifetime = get_object_lifetime(response)
        assert lifetime == S3ObjectLifetime(readable=False, readable_until=None)
        assert not lifetime.is_usable(NOW)

    def test_restored_archive_is_readable_until_restore_expiry(self):
        lifetime = get_object_lifetime(
            {"StorageClass": "GLACIER", "Restore": _completed_restore(LATER)}
        )
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=LATER)

    def test_restored_intelligent_tiering_object_without_restore_expiry(self):
        lifetime = get_object_lifetime(
            {
                "StorageClass": "INTELLIGENT_TIERING",
                "ArchiveStatus": "ARCHIVE_ACCESS",
                "Restore": 'ongoing-request="false"',
            }
        )
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=None)

    def test_expiration_sets_readable_until(self):
        lifetime = get_object_lifetime({"Expiration": _expiration(SOON)})
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=SOON)

    def test_malformed_expiration_is_ignored(self):
        lifetime = get_object_lifetime({"Expiration": 'expiry-date="nope", rule-id="r"'})
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=None)

    @pytest.mark.parametrize(
        "expiration, restore_expiry, expected",
        [
            pytest.param(SOON, LATER, SOON, id="expiration-first"),
            pytest.param(LATER, SOON, SOON, id="restore-expiry-first"),
        ],
    )
    def test_restored_archive_uses_earliest_of_expiration_and_restore_expiry(
        self, expiration, restore_expiry, expected
    ):
        lifetime = get_object_lifetime(
            {
                "StorageClass": "DEEP_ARCHIVE",
                "Restore": _completed_restore(restore_expiry),
                "Expiration": _expiration(expiration),
            }
        )
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=expected)

    def test_restore_header_on_non_archived_object_is_ignored(self):
        # A restored copy only limits access for archived objects.
        lifetime = get_object_lifetime(
            {"StorageClass": "STANDARD", "Restore": _completed_restore(SOON)}
        )
        assert lifetime == S3ObjectLifetime(readable=True, readable_until=None)


class TestS3ObjectLifetimeIsUsable:
    def test_unreadable_is_never_usable(self):
        assert not S3ObjectLifetime(readable=False, readable_until=None).is_usable(NOW)
        assert not S3ObjectLifetime(readable=False, readable_until=LATER).is_usable(NOW)

    def test_readable_without_expiry_is_usable(self):
        assert S3ObjectLifetime(readable=True, readable_until=None).is_usable(NOW)

    def test_expiry_beyond_margin_is_usable(self):
        until = NOW + OBJECT_EXPIRY_SAFETY_MARGIN + timedelta(seconds=1)
        assert S3ObjectLifetime(readable=True, readable_until=until).is_usable(NOW)

    @pytest.mark.parametrize(
        "until",
        [
            pytest.param(NOW + OBJECT_EXPIRY_SAFETY_MARGIN, id="exactly-at-margin"),
            pytest.param(NOW + timedelta(hours=1), id="within-margin"),
            pytest.param(NOW - timedelta(days=1), id="already-expired"),
        ],
    )
    def test_expiry_within_margin_is_not_usable(self, until):
        assert not S3ObjectLifetime(readable=True, readable_until=until).is_usable(NOW)

    def test_custom_margin(self):
        lifetime = S3ObjectLifetime(readable=True, readable_until=NOW + timedelta(hours=2))
        assert lifetime.is_usable(NOW, margin=timedelta(hours=1))
        assert not lifetime.is_usable(NOW, margin=timedelta(hours=3))
