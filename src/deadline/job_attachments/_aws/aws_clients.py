# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Functions for handling and retrieving AWS clients."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import boto3
import botocore
from boto3.s3.transfer import create_transfer_manager
from botocore.client import BaseClient, Config

from .. import version
from .aws_config import (
    S3_CONNECT_TIMEOUT_IN_SECS,
    S3_READ_TIMEOUT_IN_SECS,
    S3_RETRIES_MODE,
    VENDOR_CODE,
)

MAX_SIZE_CACHE = 128

logger = logging.getLogger("deadline.job_attachments")


# Should create a new botocore session since botocore session may be modified by boto3 session/client using it
# https://github.com/boto/boto3/blob/61de529b5f9a7bdcc8c76debb472a7f934d048e6/boto3/session.py#L79
def get_botocore_session() -> botocore.session.Session:
    session = botocore.session.get_session()
    # Use regional endpoints by default for STS and S3 (us-east-1) to avoid
    # cross-region calls to the global endpoint. This is the default in newer verisons,
    # but older botocore versions default to "legacy" which routes through us-east-1.
    session.set_config_variable("sts_regional_endpoints", "regional")
    session.set_config_variable("s3", {"us_east_1_regional_endpoint": "regional"})
    return session


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_boto3_session(
    botocore_session: botocore.session.Session = get_botocore_session(),
) -> boto3.session.Session:
    return boto3.session.Session(botocore_session=botocore_session)


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_deadline_client(
    session: Optional[boto3.session.Session] = None, endpoint_url: Optional[str] = None
) -> BaseClient:
    """
    Get a boto3 Deadline client to make API calls to Deadline
    """
    if session is None:
        session = get_boto3_session()

    return session.client(VENDOR_CODE, endpoint_url=endpoint_url)


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_s3_client(
    session: Optional[boto3.Session] = None, s3_max_pool_connections: int = 50
) -> BaseClient:
    """
    Get a boto3 S3 client to make API calls to S3
    """
    if session is None:
        session = get_boto3_session()

    client = session.client(
        "s3",
        config=Config(
            signature_version="s3v4",
            connect_timeout=S3_CONNECT_TIMEOUT_IN_SECS,
            read_timeout=S3_READ_TIMEOUT_IN_SECS,
            retries={"mode": S3_RETRIES_MODE},
            user_agent_extra=f"S3A/Deadline/NA/JobAttachments/{version}",
            max_pool_connections=s3_max_pool_connections,
        ),
    )

    def add_expected_bucket_owner(params, model, **kwargs):
        """
        Add the expected bucket owner to the params if the API operation to run can use it.
        """
        if "ExpectedBucketOwner" in model.input_shape.members:
            account_id = get_account_id(session=session)
            if account_id:
                params["ExpectedBucketOwner"] = account_id

    client.meta.events.register("provide-client-params.s3.*", add_expected_bucket_owner)

    return client


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_s3_transfer_manager(s3_client: BaseClient):
    transfer_config = boto3.s3.transfer.TransferConfig()
    return create_transfer_manager(client=s3_client, config=transfer_config)


@lru_cache(maxsize=MAX_SIZE_CACHE)
def get_account_id(session: Optional[boto3.session.Session] = None) -> Optional[str]:
    """
    Get the account id for the current session, or ``None`` if it cannot be determined.

    Sources the account from the session's frozen credentials, which botocore populates
    automatically for credential providers that know the account (e.g. AssumeRole extracts
    it from the assumed-role ARN, and static credentials can be paired with
    ``AWS_ACCOUNT_ID`` / ``aws_account_id`` in config). Falls back to ``sts:GetCallerIdentity``
    only when the credential provider did not supply an account id.
    """
    if session is None:
        session = get_boto3_session()

    credentials = session.get_credentials()
    if credentials is not None:
        frozen = credentials.get_frozen_credentials()
        account_id = getattr(frozen, "account_id", None)
        if account_id:
            return account_id

    # Fallback for older botocore or credential providers that don't populate account_id
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception:
        logger.debug(
            "Could not determine AWS account ID from session credentials or STS. "
            "S3 requests will not include the ExpectedBucketOwner header."
        )
        return None
