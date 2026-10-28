# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Helpers for interpreting the lifecycle-related metadata that S3 HeadObject returns, so that
uploads and the S3 check cache agree with what S3 Lifecycle rules will actually do to an object.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional

logger = logging.getLogger("Deadline")

# Storage classes whose objects can't be read with GetObject until they are restored.
ARCHIVE_STORAGE_CLASSES = frozenset({"GLACIER", "DEEP_ARCHIVE"})
# S3 Intelligent-Tiering access tiers whose objects can't be read until they are restored.
ARCHIVE_ACCESS_TIERS = frozenset({"ARCHIVE_ACCESS", "DEEP_ARCHIVE_ACCESS"})

# Objects that will expire (or whose restored copy will expire) within this margin are treated as
# missing. This leaves time for a submitted job to start and download its attachments before S3
# removes them.
OBJECT_EXPIRY_SAFETY_MARGIN = timedelta(days=1)

_EXPIRY_DATE_RE = re.compile(r'expiry-date="([^"]*)"')
_ONGOING_REQUEST_RE = re.compile(r'ongoing-request="([^"]*)"')


@dataclass(frozen=True)
class S3ObjectLifetime:
    """What a HeadObject response says about whether, and until when, an object can be downloaded."""

    readable: bool
    """False if the object is archived and has no completed restore, so GetObject would fail."""
    readable_until: Optional[datetime]
    """When a lifecycle expiration or a restore expiry removes access to the object. None if unknown."""

    def is_usable(self, now: datetime, margin: timedelta = OBJECT_EXPIRY_SAFETY_MARGIN) -> bool:
        """Whether the object is readable now and will stay readable for at least `margin`."""
        if not self.readable:
            return False
        return self.readable_until is None or now + margin < self.readable_until


def _parse_expiry_date(header_value: Optional[str]) -> Optional[datetime]:
    """
    Parses the expiry-date field of an x-amz-expiration or x-amz-restore header value, such as
    'expiry-date="Fri, 23 Dec 2012 00:00:00 GMT", rule-id="rule1"'. Returns a timezone-aware
    datetime, or None if the field is missing or malformed.
    """
    if not isinstance(header_value, str):
        return None
    match = _EXPIRY_DATE_RE.search(header_value)
    if not match:
        return None
    try:
        expiry = parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError, IndexError):
        # Python < 3.10 raises TypeError for unparseable dates; later versions raise ValueError.
        logger.debug(f"Ignoring unparseable S3 expiry date in header value: {header_value}")
        return None
    if expiry.tzinfo is None:
        # parsedate_to_datetime returns a naive datetime for a "-0000" zone. HTTP dates are GMT.
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry


def _is_restore_completed(restore_header: Optional[str]) -> bool:
    """Whether an x-amz-restore header value describes a completed restore."""
    if not isinstance(restore_header, str):
        return False
    match = _ONGOING_REQUEST_RE.search(restore_header)
    return match is not None and match.group(1).lower() == "false"


def get_object_lifetime(head_object_response: Mapping[str, Any]) -> S3ObjectLifetime:
    """
    Interprets a boto3 head_object response. Uses only fields that HeadObject already returns, so
    no extra permissions or API calls are needed:

    - StorageClass / ArchiveStatus: whether the object is in an archive storage class or tier.
    - Restore: whether an archived object has a completed restore, and when the restored copy expires.
    - Expiration: when a lifecycle expiration rule will delete the object.
    """
    readable_until = _parse_expiry_date(head_object_response.get("Expiration"))

    is_archived = (
        head_object_response.get("StorageClass") in ARCHIVE_STORAGE_CLASSES
        or head_object_response.get("ArchiveStatus") in ARCHIVE_ACCESS_TIERS
    )
    if is_archived:
        restore_header = head_object_response.get("Restore")
        if not _is_restore_completed(restore_header):
            return S3ObjectLifetime(readable=False, readable_until=None)
        restore_expiry = _parse_expiry_date(restore_header)
        if restore_expiry is not None and (
            readable_until is None or restore_expiry < readable_until
        ):
            readable_until = restore_expiry

    return S3ObjectLifetime(readable=True, readable_until=readable_until)
