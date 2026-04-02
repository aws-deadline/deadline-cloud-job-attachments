# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# https://docs.getmoto.org/en/latest/docs/getting_started.html#pesky-imports-section
from moto import mock_aws  # noqa: F401
import getpass
import sys
import pytest
import tempfile

from pathlib import Path


def pytest_xdist_auto_num_workers(config):
    # Disable xdist on Windows + Python 3.14+ due to module import race conditions in workers
    if sys.platform == "win32" and sys.version_info >= (3, 14):
        return 1


@pytest.fixture(scope="function", autouse=True)
def aws_config(monkeypatch):
    """
    Fixture to set the AWS_CONFIG_FILE environment variable to a temporary file.

    This fixture sanitizes the environment, otherwise it's easy to write a test that only works when
    a AWS config file exists(even if it isn't used) that then fails on machines where no such config file exists

    This fixture yields the AWS config file path, so that tests can write to this file if necessary for testing
    """
    try:
        temp_dir = tempfile.TemporaryDirectory()
        temp_dir_path = Path(temp_dir.name)
        temp_file_path = temp_dir_path / "aws_config"
        monkeypatch.setenv("AWS_CONFIG_FILE", str(temp_file_path))
        monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)

        yield temp_file_path

    finally:
        temp_dir.cleanup()
        monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)


def is_windows_non_admin():
    return sys.platform == "win32" and getpass.getuser() != "Administrator"
