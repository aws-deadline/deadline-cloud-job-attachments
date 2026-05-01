# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for aws clients"""

import os
import tempfile
from collections import namedtuple
from io import BytesIO
from unittest.mock import Mock, patch, MagicMock

import boto3
import pytest
from boto3.s3.transfer import create_transfer_manager, TransferConfig
from botocore.exceptions import ClientError
from moto import mock_aws

from deadline.job_attachments._aws.aws_clients import (
    get_account_id,
    get_boto3_session,
    get_botocore_session,
    get_deadline_client,
    get_s3_client,
)
import deadline
from deadline.job_attachments._aws.aws_config import (
    S3_CONNECT_TIMEOUT_IN_SECS,
    S3_READ_TIMEOUT_IN_SECS,
)


def _make_client(service_name, session=None):
    """Create a client using the production factory functions with an optional fresh session."""
    if session is None:
        session = get_boto3_session(get_botocore_session())
    factories = {
        "s3": get_s3_client,
        "deadline": get_deadline_client,
    }
    return factories[service_name](session=session)


def test_get_deadline_client(boto_config):
    """
    Test that get_deadline_client returns the correct deadline client
    """
    session_mock = Mock()
    with patch(
        f"{deadline.__package__}.job_attachments._aws.aws_clients.get_boto3_session"
    ) as get_session:
        get_session.return_value = session_mock
        session_mock.client.return_value = Mock()
        get_deadline_client()

    session_mock.client.assert_called_with("deadline", endpoint_url=None)


def test_get_deadline_client_non_default_endpoint(boto_config):
    """
    Test that get_deadline_client returns the correct deadline client
    and that the endpoint url is the given one when provided.
    """
    test_endpoint = "https://test.com"
    session_mock = Mock()
    with patch(
        f"{deadline.__package__}.job_attachments._aws.aws_clients.get_boto3_session"
    ) as get_session:
        get_session.return_value = session_mock
        session_mock.client.return_value = Mock()
        get_deadline_client(endpoint_url=test_endpoint)

    session_mock.client.assert_called_with("deadline", endpoint_url=test_endpoint)


def test_get_s3_client(boto_config):
    """
    Test that get_s3_client returns a properly configured S3 client.
    """
    s3_client = get_s3_client()

    assert s3_client.meta.config.signature_version == "s3v4"
    assert s3_client.meta.config.connect_timeout == S3_CONNECT_TIMEOUT_IN_SECS
    assert s3_client.meta.config.read_timeout == S3_READ_TIMEOUT_IN_SECS


@pytest.mark.parametrize("service_name", ["s3", "deadline"])
def test_default_regional_endpoint(boto_config, service_name):
    """
    Test that S3 and STS clients (previously global by default) now use regional endpoints by default.
    """
    region = os.environ["AWS_DEFAULT_REGION"]
    client = _make_client(service_name)
    assert client.meta.endpoint_url == f"https://{service_name}.{region}.amazonaws.com"


@pytest.mark.parametrize(
    "service_name, env_var",
    [
        ("s3", "AWS_ENDPOINT_URL_S3"),
        ("deadline", "AWS_ENDPOINT_URL_DEADLINE"),
    ],
)
def test_endpoint_url_override_via_env(boto_config, service_name, env_var):
    """
    Test that clients respect service-specific AWS_ENDPOINT_URL_* environment variables.
    """
    custom_endpoint = f"https://custom-{service_name}-env.example.com"
    with patch.dict(os.environ, {env_var: custom_endpoint}):
        client = _make_client(service_name)
        assert client.meta.endpoint_url == custom_endpoint


@pytest.mark.parametrize(
    "service_name",
    ["s3", "deadline"],
)
def test_endpoint_url_override_via_config_profile(boto_config, tmp_path, service_name):
    """
    Test that clients respect endpoint_url set in an AWS config profile.
    """
    custom_endpoint = f"https://custom-{service_name}-config.example.com"
    config_file = tmp_path / "config"
    config_file.write_text(f"""
[profile testprofile]
services = testprofile-services

[services testprofile-services]
{service_name} =
    endpoint_url = {custom_endpoint}
""")
    with patch.dict(
        os.environ,
        {
            "AWS_CONFIG_FILE": str(config_file),
            "AWS_PROFILE": "testprofile",
        },
    ):
        client = _make_client(service_name)
        assert client.meta.endpoint_url == custom_endpoint


class TestGetAccountId:
    """Tests for get_account_id credential-based lookup and STS fallback."""

    def setup_method(self):
        get_account_id.cache_clear()

    def test_returns_account_from_frozen_credentials(self, boto_config):
        """When frozen credentials carry an account_id, return it without calling STS."""
        FakeFrozen = namedtuple("FakeFrozen", ["access_key", "secret_key", "token", "account_id"])
        frozen = FakeFrozen("AK", "SK", "tok", "111122223333")

        mock_session = MagicMock()
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = frozen
        mock_session.get_credentials.return_value = mock_creds

        assert get_account_id(session=mock_session) == "111122223333"
        mock_session.client.assert_not_called()

    def test_falls_back_to_sts_when_no_account_on_credentials(self, boto_config):
        """When frozen credentials have no account_id, fall back to sts:GetCallerIdentity."""
        FakeFrozen = namedtuple("FakeFrozen", ["access_key", "secret_key", "token"])
        frozen = FakeFrozen("AK", "SK", "tok")

        mock_session = MagicMock()
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = frozen
        mock_session.get_credentials.return_value = mock_creds

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "444455556666"}
        mock_session.client.return_value = mock_sts

        assert get_account_id(session=mock_session) == "444455556666"
        mock_session.client.assert_called_once_with("sts")

    def test_falls_back_to_sts_when_account_id_is_none(self, boto_config):
        """When frozen credentials have account_id=None, fall back to STS."""
        FakeFrozen = namedtuple("FakeFrozen", ["access_key", "secret_key", "token", "account_id"])
        frozen = FakeFrozen("AK", "SK", "tok", None)

        mock_session = MagicMock()
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = frozen
        mock_session.get_credentials.return_value = mock_creds

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "777788889999"}
        mock_session.client.return_value = mock_sts

        assert get_account_id(session=mock_session) == "777788889999"

    def test_returns_none_when_sts_fails(self, boto_config):
        """When credentials have no account_id and STS call fails, return None."""
        FakeFrozen = namedtuple("FakeFrozen", ["access_key", "secret_key", "token"])
        frozen = FakeFrozen("AK", "SK", "tok")

        mock_session = MagicMock()
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = frozen
        mock_session.get_credentials.return_value = mock_creds

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.side_effect = ClientError(
            {"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "GetCallerIdentity"
        )
        mock_session.client.return_value = mock_sts

        assert get_account_id(session=mock_session) is None

    def test_returns_none_when_no_credentials(self, boto_config):
        """When session has no credentials at all and STS fails, return None."""
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.side_effect = Exception("no creds")
        mock_session.client.return_value = mock_sts

        assert get_account_id(session=mock_session) is None


class _S3ParamCapture:
    """Helper that registers an event listener on an S3 client to capture params from all operations."""

    def __init__(self, s3_client):
        self.params = {}
        s3_client.meta.events.register("provide-client-params.s3.*", self._capture)

    def _capture(self, params, **kwargs):
        self.params.update(params)


class TestExpectedBucketOwnerHeader:
    """Tests that the S3 client event handler correctly sets ExpectedBucketOwner."""

    @mock_aws
    def test_expected_bucket_owner_set_on_s3_calls(self, boto_config):
        """When get_account_id returns an account, S3 calls include ExpectedBucketOwner."""
        session = boto3.Session(region_name="us-west-2")
        s3_client = get_s3_client(session=session)
        capture = _S3ParamCapture(s3_client)

        s3_client.create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        s3_client.put_object(Bucket="test-bucket", Key="test-key", Body=b"data")

        with patch(
            f"{deadline.__package__}.job_attachments._aws.aws_clients.get_account_id",
            return_value="123456789012",
        ):
            capture.params.clear()
            s3_client.get_object(Bucket="test-bucket", Key="test-key")

        assert capture.params.get("ExpectedBucketOwner") == "123456789012"

    @mock_aws
    def test_expected_bucket_owner_omitted_when_no_account(self, boto_config):
        """When get_account_id returns None, S3 calls do NOT include ExpectedBucketOwner."""
        session = boto3.Session(region_name="us-west-2")
        s3_client = get_s3_client(session=session)
        capture = _S3ParamCapture(s3_client)

        s3_client.create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        s3_client.put_object(Bucket="test-bucket", Key="test-key", Body=b"data")

        with patch(
            f"{deadline.__package__}.job_attachments._aws.aws_clients.get_account_id",
            return_value=None,
        ):
            capture.params.clear()
            s3_client.get_object(Bucket="test-bucket", Key="test-key")

        assert "ExpectedBucketOwner" not in capture.params

    @mock_aws
    def test_expected_bucket_owner_on_transfer_manager_download(self, boto_config):
        """The event handler fires for transfer manager download operations."""
        session = boto3.Session(region_name="us-west-2")
        s3_client = get_s3_client(session=session)
        capture = _S3ParamCapture(s3_client)

        s3_client.create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        s3_client.put_object(Bucket="test-bucket", Key="test-key", Body=b"data")

        tm = create_transfer_manager(client=s3_client, config=TransferConfig())

        with patch(
            f"{deadline.__package__}.job_attachments._aws.aws_clients.get_account_id",
            return_value="999988887777",
        ):
            capture.params.clear()
            with tempfile.NamedTemporaryFile(delete=False) as f:
                tmp = f.name
            future = tm.download(bucket="test-bucket", key="test-key", fileobj=tmp)
            future.result()
            os.unlink(tmp)

        assert capture.params.get("ExpectedBucketOwner") == "999988887777"

    @mock_aws
    def test_expected_bucket_owner_on_upload_fileobj(self, boto_config):
        """The event handler fires for upload_fileobj (PutObject) operations."""
        session = boto3.Session(region_name="us-west-2")
        s3_client = get_s3_client(session=session)
        capture = _S3ParamCapture(s3_client)

        s3_client.create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )

        with patch(
            f"{deadline.__package__}.job_attachments._aws.aws_clients.get_account_id",
            return_value="111122223333",
        ):
            capture.params.clear()
            s3_client.upload_fileobj(BytesIO(b"hello"), "test-bucket", "test-key")

        assert capture.params.get("ExpectedBucketOwner") == "111122223333"
