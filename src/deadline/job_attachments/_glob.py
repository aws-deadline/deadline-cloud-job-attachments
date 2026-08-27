# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import glob
import json
from pathlib import Path
from typing import List, Optional
from deadline.job_attachments._utils import (
    _as_extended_length_path,
    _normalize_windows_path,
)
from deadline.job_attachments.exceptions import NonValidInputError
from deadline.job_attachments.models import GlobConfig


def _process_glob_inputs(glob_arg_input: str) -> GlobConfig:
    """
    Helper function to process glob inputs.
    glob_input: String, can represent a json, filepath, or general include glob syntax.
    """

    # Default Glob config.
    glob_config = GlobConfig()
    if glob_arg_input is None or len(glob_arg_input) == 0:
        # Not configured, or not passed in.
        return glob_config

    try:
        input_as_path = Path(glob_arg_input)
        if input_as_path.is_file():
            # Read the file so it can be parsed as JSON.
            with open(glob_arg_input) as f:
                glob_arg_input = f.read()
    except Exception:
        # If this cannot be processed as a file, try it as JSON.
        pass

    try:
        # Parse the input as JSON, default to Glob Config defaults.
        input_as_json = json.loads(glob_arg_input)
        glob_config.include_glob = input_as_json.get(GlobConfig.INCLUDE, glob_config.include_glob)
        glob_config.exclude_glob = input_as_json.get(GlobConfig.EXCLUDE, glob_config.exclude_glob)
    except Exception:
        # This is not a JSON blob, bad input.
        raise NonValidInputError(f"Glob input {glob_arg_input} cannot be deserialized as JSON")

    return glob_config


def _match_files_with_pattern(base_path: str, patterns: List[str]) -> set:
    """
    Helper function to match files based on glob patterns.

    Args:
        base_path: Root path to glob from
        patterns: List of glob patterns to match

    Returns:
        Set of normalized file paths that match the patterns
    """
    matched_files = set()
    for pattern in patterns:
        # Make pattern relative to base path
        full_pattern = os.path.join(base_path, pattern)

        # Use recursive glob for directory matching
        for matched_path in glob.glob(full_pattern, recursive=True):
            # Only add files, not directories
            if os.path.isfile(matched_path):
                # Convert to proper path format
                normalized_path = os.path.normpath(matched_path)
                matched_files.add(normalized_path)

    return matched_files


def _glob_paths(
    path: str, include: List[str] = ["**/*"], exclude: Optional[List[str]] = None
) -> List[str]:
    """
    Glob routine that supports Unix style pathname pattern expansion for includes and excludes.
    This function will recursively list all files of path, including all files globbed by include and removing all files marked by exclude.
    path: Root path to glob.
    include: Optional, pattern syntax for files to include.
    exclude: Optional, pattern syntax for files to exclude.
    return: List of files found based on supplied glob patterns.
    """
    # Convert path to absolute path
    base_path = os.path.abspath(path)

    # Walk under the extended-length form so callers that reach this from a process
    # without a longPathAware manifest, or on a host with the LongPathsEnabled registry
    # setting off, can still descend past MAX_PATH. glob.glob on a plain long root
    # truncates silently and yields nothing, so a snapshot completes with an empty
    # manifest -- reported as success but capturing no files. Reachable in the worker
    # via the registry-off configuration (its python.exe subprocess is longPathAware but
    # the registry gate still applies), and in DCC-embedded interpreters that call the
    # CLI's manifest-snapshot path. No-op on POSIX and for already-prefixed roots.
    walk_root = str(_as_extended_length_path(base_path))

    # Process include patterns
    matched_files = _match_files_with_pattern(walk_root, include)

    # Process exclude patterns
    if exclude:
        files_to_exclude = _match_files_with_pattern(walk_root, exclude)
        # Remove excluded files from result
        matched_files -= files_to_exclude

    # Strip the prefix on the way out. Downstream keeps the plain form as its manifest
    # keys and containment-check inputs, and re-applies the prefix per file when needed
    # via _get_long_path_compatible_path.
    return [str(_normalize_windows_path(p)) for p in matched_files]
