# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os

from contextlib import ExitStack
from typing import List, Dict

from .._utils import _get_long_path_compatible_path
from ..exceptions import NonValidInputError
from ..asset_manifests.base_manifest import BaseAssetManifest
from ..asset_manifests.decode import decode_manifest


def _read_manifests(manifest_paths: List[str]) -> Dict[str, BaseAssetManifest]:
    """
    Read in manfiests from the given file path list, and produce file name to manifest mapping.

    Args:
        manifest_paths (List[str]): List of file paths to manifest file.

    Raises:
        NonValidInputError: Raise when any of the file is not valid.

    Returns:
        Dict[str, BaseAssetManifest]: File name to encoded manifest mapping
    """

    # Callers are handed plain (unprefixed) manifest paths -- _write_manifest returns the
    # user-facing form -- so the prefix has to be re-applied for the filesystem calls
    # here. Without it, a manifest written to a >MAX_PATH destination stats as missing and
    # is reported as "not valid". The dict keys stay derived from the plain path so the
    # prefix never reaches a return value.
    #
    # abspath first because, unlike every other caller of the helper, these paths come
    # straight from the CLI and are not resolved. \\?\ requires a fully qualified path and
    # disables the normalization that would otherwise resolve a relative one, so a long
    # relative path would be prefixed into something the filesystem rejects. abspath also
    # collapses ".." against the real cwd, so the helper's ".." guard -- which exists to
    # catch callers that skipped resolution -- does not surface a bare ValueError here in
    # place of the NonValidInputError this function is documented to raise.
    read_paths = {
        manifest: str(_get_long_path_compatible_path(os.path.abspath(manifest)))
        for manifest in manifest_paths
    }

    if nonvalid_files := [
        manifest for manifest, read_path in read_paths.items() if not os.path.isfile(read_path)
    ]:
        raise NonValidInputError(f"Specified manifests {nonvalid_files} are not valid.")

    with ExitStack() as stack:
        file_name_manifest_dict: Dict[str, BaseAssetManifest] = {
            os.path.basename(file_path): decode_manifest(
                stack.enter_context(open(read_paths[file_path])).read()
            )
            for file_path in manifest_paths
        }

    return file_name_manifest_dict
