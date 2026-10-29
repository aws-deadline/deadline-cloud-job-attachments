# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from deadline.job_attachments.models import ManifestProperties, PathFormat
from deadline.job_attachments.session_info import (
    SESSION_INFO_ENV_VAR,
    SESSION_INFO_FILE_NAME,
    SESSION_INFO_VERSION,
    SessionAssetRoot,
    SessionJobAttachmentsInfo,
    read_session_info,
    write_session_info,
)


def _info() -> SessionJobAttachmentsInfo:
    return SessionJobAttachmentsInfo(
        roots=[
            SessionAssetRoot(
                root_path="/home/artist/project",
                root_path_format=PathFormat.POSIX,
                local_root_path="/sessions/session-0123/assetroot-abc",
                output_relative_directories=["renders/beauty", "comp"],
            ),
            SessionAssetRoot(
                root_path="C:\\projects\\shared",
                root_path_format=PathFormat.WINDOWS,
                local_root_path="/mnt/shared",
                file_system_location_name="Shared",
            ),
        ]
    )


def test_to_dict() -> None:
    assert _info().to_dict() == {
        "version": SESSION_INFO_VERSION,
        "roots": [
            {
                "rootPath": "/home/artist/project",
                "rootPathFormat": "posix",
                "localRootPath": "/sessions/session-0123/assetroot-abc",
                "outputRelativeDirectories": ["renders/beauty", "comp"],
                "localOutputDirectories": [
                    os.path.normpath("/sessions/session-0123/assetroot-abc/renders/beauty"),
                    os.path.normpath("/sessions/session-0123/assetroot-abc/comp"),
                ],
            },
            {
                "rootPath": "C:\\projects\\shared",
                "rootPathFormat": "windows",
                "localRootPath": "/mnt/shared",
                "outputRelativeDirectories": [],
                "localOutputDirectories": [],
                "fileSystemLocationName": "Shared",
            },
        ],
    }


def test_round_trip() -> None:
    info = _info()
    assert SessionJobAttachmentsInfo.from_dict(json.loads(json.dumps(info.to_dict()))) == info


def test_from_dict_accepts_newer_minor_version_and_unknown_keys() -> None:
    data = {
        "version": "jobattachments-session-info-1.7",
        "futureTopLevelKey": True,
        "roots": [
            {
                "rootPath": "/project",
                "rootPathFormat": "posix",
                "localRootPath": "/sessions/s/assetroot-1",
                "futureRootKey": "value",
            }
        ],
    }
    info = SessionJobAttachmentsInfo.from_dict(data)
    assert info.roots == [
        SessionAssetRoot(
            root_path="/project",
            root_path_format=PathFormat.POSIX,
            local_root_path="/sessions/s/assetroot-1",
        )
    ]


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"roots": []},
        {"version": "pathmapping-1.0", "roots": []},
        {"version": "jobattachments-session-info-2.0", "roots": []},
        {"version": SESSION_INFO_VERSION},
        {"version": SESSION_INFO_VERSION, "roots": [{"rootPath": "/project"}]},
        {"version": SESSION_INFO_VERSION, "roots": None},
        {
            "version": SESSION_INFO_VERSION,
            "roots": [{"rootPath": "/p", "rootPathFormat": "amiga", "localRootPath": "/s"}],
        },
        {"version": SESSION_INFO_VERSION, "roots": {"rootPath": "/p"}},
        {
            "version": SESSION_INFO_VERSION,
            "roots": [{"rootPath": 1, "rootPathFormat": "posix", "localRootPath": "/s"}],
        },
        {
            "version": SESSION_INFO_VERSION,
            "roots": [{"rootPath": "/p", "rootPathFormat": "posix", "localRootPath": None}],
        },
        {
            "version": SESSION_INFO_VERSION,
            "roots": [
                {
                    "rootPath": "/p",
                    "rootPathFormat": "posix",
                    "localRootPath": "/s",
                    "fileSystemLocationName": 3,
                }
            ],
        },
        {
            "version": SESSION_INFO_VERSION,
            "roots": [
                {
                    "rootPath": "/p",
                    "rootPathFormat": "posix",
                    "localRootPath": "/s",
                    "outputRelativeDirectories": "renders",
                }
            ],
        },
        {
            "version": SESSION_INFO_VERSION,
            "roots": [
                {
                    "rootPath": "/p",
                    "rootPathFormat": "posix",
                    "localRootPath": "/s",
                    "outputRelativeDirectories": ["renders", 1],
                }
            ],
        },
    ],
)
def test_from_dict_rejects_unreadable_data(data) -> None:
    with pytest.raises(ValueError):
        SessionJobAttachmentsInfo.from_dict(data)


def test_from_manifest_properties() -> None:
    manifest_properties = ManifestProperties(
        rootPath="/home/artist/project",
        rootPathFormat=PathFormat.POSIX,
        fileSystemLocationName="Project",
        inputManifestPath="farm/queue/job/manifest_input",
        inputManifestHash="abc",
        outputRelativeDirectories=["renders"],
    )
    assert SessionAssetRoot.from_manifest_properties(
        manifest_properties, "/sessions/s/assetroot-1"
    ) == SessionAssetRoot(
        root_path="/home/artist/project",
        root_path_format=PathFormat.POSIX,
        local_root_path="/sessions/s/assetroot-1",
        file_system_location_name="Project",
        output_relative_directories=["renders"],
    )


def test_from_manifest_properties_without_outputs() -> None:
    manifest_properties = ManifestProperties(rootPath="/project", rootPathFormat=PathFormat.POSIX)
    root = SessionAssetRoot.from_manifest_properties(manifest_properties, "/local")
    assert root.output_relative_directories == []


@pytest.mark.parametrize(
    ("root_path_format", "output_dir", "expected_parts"),
    [
        (PathFormat.POSIX, "renders/beauty", ["renders", "beauty"]),
        (PathFormat.WINDOWS, "renders\\beauty", ["renders", "beauty"]),
        (PathFormat.POSIX, ".", []),
    ],
)
def test_local_output_directories(
    tmp_path: Path, root_path_format: PathFormat, output_dir: str, expected_parts
) -> None:
    root = SessionAssetRoot(
        root_path="/project",
        root_path_format=root_path_format,
        local_root_path=str(tmp_path),
        output_relative_directories=[output_dir],
    )
    assert root.local_output_directories() == [
        os.path.normpath(os.path.join(str(tmp_path), *expected_parts))
    ]


@pytest.mark.parametrize(
    ("root_path_format", "output_dir"),
    [
        (PathFormat.POSIX, "../outside"),
        (PathFormat.POSIX, "renders/../../outside"),
        (PathFormat.POSIX, "/etc"),
        (PathFormat.WINDOWS, "..\\..\\outside"),
        (PathFormat.WINDOWS, "C:\\absolute"),
        (PathFormat.WINDOWS, "\\\\server\\share"),
    ],
)
def test_local_output_directories_skips_paths_outside_root(
    tmp_path: Path, root_path_format: PathFormat, output_dir: str
) -> None:
    root = SessionAssetRoot(
        root_path="/project",
        root_path_format=root_path_format,
        local_root_path=str(tmp_path / "root"),
        output_relative_directories=[output_dir, "inside"],
    )
    assert root.local_output_directories() == [os.path.normpath(str(tmp_path / "root" / "inside"))]


def test_from_dict_ignores_local_output_directories() -> None:
    root = _info().roots[0]
    data = root.to_dict()
    data["localOutputDirectories"] = ["/somewhere/else"]
    assert SessionAssetRoot.from_dict(data) == root


def test_write_and_read(tmp_path: Path) -> None:
    info = _info()
    path = write_session_info(info, tmp_path)
    assert path == tmp_path / SESSION_INFO_FILE_NAME
    assert [p.name for p in tmp_path.iterdir()] == [SESSION_INFO_FILE_NAME]
    assert read_session_info(path) == info


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_write_honors_umask(tmp_path: Path) -> None:
    old_umask = os.umask(0o027)
    try:
        path = write_session_info(_info(), tmp_path)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_write_failure_removes_temporary_file(tmp_path: Path) -> None:
    with patch("deadline.job_attachments._session_info.json.dump", side_effect=OSError("full")):
        with pytest.raises(OSError):
            write_session_info(_info(), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Creating symlinks needs privileges on Windows")
def test_write_does_not_follow_planted_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (tmp_path / SESSION_INFO_FILE_NAME).symlink_to(target)
    path = write_session_info(_info(), tmp_path)
    assert not path.is_symlink()
    assert not target.exists()


def test_write_replaces_existing_file(tmp_path: Path) -> None:
    write_session_info(_info(), tmp_path)
    path = write_session_info(SessionJobAttachmentsInfo(), tmp_path)
    assert read_session_info(path) == SessionJobAttachmentsInfo()


def test_read_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_session_info(_info(), tmp_path)
    monkeypatch.setenv(SESSION_INFO_ENV_VAR, str(path))
    assert read_session_info() == _info()


def test_read_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_INFO_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match=SESSION_INFO_ENV_VAR):
        read_session_info()


def test_read_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / SESSION_INFO_FILE_NAME
    path.write_bytes(b"\xff\xfe{")
    with pytest.raises(ValueError):
        read_session_info(path)


def test_read_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / SESSION_INFO_FILE_NAME
    path.write_text("{not json")
    with pytest.raises(ValueError, match="valid JSON"):
        read_session_info(path)
