# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import datetime
from functools import lru_cache, wraps
from hashlib import shake_256
import ntpath
from pathlib import Path, PureWindowsPath
import random
import time
from typing import Any, Callable, Optional, Tuple, Type, Union
import uuid
import sys

__all__ = [
    "_join_s3_paths",
    "_generate_random_guid",
    "_float_to_iso_datetime_string",
    "_get_unique_dest_dir_name",
    "_get_bucket_and_object_key",
    "_is_relative_to",
]


TEMP_DOWNLOAD_ADDED_CHARS_LENGTH = 9
"""
Add 9 to path length to account for .Hex value when file is in the middle of downloading in windows.
e.g. test.txt when downloaded becomes test.txt.H4SD9Ddj
"""

WINDOWS_MAX_PATH_LENGTH = 260
"""
Windows Max path length limit of 260.
https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation
"""

WINDOWS_PATH_SEPARATOR = "\\"
"""
The Windows path separator, spelled out because these helpers parse and build Windows
paths on any host: the win32 branch below is exercised on POSIX in tests that patch
sys.platform, where os.sep would be "/".
"""

WINDOWS_UNC_PATH_STRING_PREFIX = "\\\\?\\"
"""
When this is prepended to any path on Windows,
it becomes a UNC path and is allowed to go over the 260 max path length limit.
"""

WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX = "\\\\?\\UNC\\"
"""
The equivalent prefix for a network path (\\\\server\\share). The leading pair of
backslashes is replaced by this prefix, giving \\\\?\\UNC\\server\\share. Prepending
the drive-letter form verbatim would produce \\\\?\\\\\\server\\share, which Windows
rejects.
"""


def _join_s3_paths(root: str, *args: str):
    return "/".join([root, *args])


def _generate_random_guid():
    return str(uuid.uuid4()).replace("-", "")


def _float_to_iso_datetime_string(time: float):
    seconds = int(time)
    microseconds = int((time - seconds) * 1000000)

    dt = datetime.datetime.utcfromtimestamp(seconds) + datetime.timedelta(microseconds=microseconds)
    iso_string = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    return iso_string


def _get_unique_dest_dir_name(source_root: str) -> str:
    # Note: this is a quick naive way to attempt to prevent colliding
    # relative paths across manifests without adding too much
    # length to the filepaths. length = 2n where n is the number
    # passed to hexdigest.
    return f"assetroot-{shake_256(source_root.encode()).hexdigest(10)}"


def _get_bucket_and_object_key(s3_path: str) -> Tuple[str, str]:
    """Returns the bucket name and object key from the S3 URI"""
    bucket, key = s3_path.replace("s3://", "").split("/", maxsplit=1)
    return bucket, key


def _normalize_windows_path(path: Union[Path, str]) -> Path:
    """
    Strips the \\\\?\\ or \\\\?\\UNC\\ prefix from Windows paths.
    """
    p_str = str(path)
    if p_str.startswith(WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX):
        # Restore the leading pair of backslashes that the \\?\UNC\ form replaced, so the
        # result is \\server\share again. Stripping the prefix outright would leave a
        # network path looking relative, which would then compare unequal against the
        # same path in its normal form. os.sep is not used here because this function
        # parses Windows paths regardless of the running platform.
        return Path(
            WINDOWS_PATH_SEPARATOR * 2 + p_str[len(WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX) :]
        )
    if p_str.startswith(WINDOWS_UNC_PATH_STRING_PREFIX):
        return Path(p_str[len(WINDOWS_UNC_PATH_STRING_PREFIX) :])
    return Path(path)


def _is_relative_to(path1: Union[Path, str], path2: Union[Path, str]) -> bool:
    """
    Determines if path1 is relative to path2. This function is to support
    Python versions (3.7 and 3.8) that do not have the built-in `Path.is_relative_to()` method.
    """
    try:
        p1 = _normalize_windows_path(Path(path1).resolve())
        p2 = _normalize_windows_path(Path(path2).resolve())
        p1.relative_to(p2)
        return True
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _is_windows_long_path_registry_enabled() -> bool:
    # Cached: the RtlAreLongPathsEnabled value is latched per-process and the
    # uncached path constructs a fresh ctypes.WinDLL on every call.
    if sys.platform != "win32":
        return True

    import ctypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
    ntdll.RtlAreLongPathsEnabled.argtypes = ()

    return bool(ntdll.RtlAreLongPathsEnabled())


def _get_long_path_compatible_path(original_path: Union[str, Path]) -> Path:
    """
    Given a Path or string representing a path,
    make it long path compatible if needed on Windows and return the Path object
    https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation

    The prefix is applied whenever the path is long, without consulting
    _is_windows_long_path_registry_enabled.

    That helper calls RtlAreLongPathsEnabled, which despite its name reports the
    *process's* effective state -- the registry value AND the calling executable's
    longPathAware manifest declaration -- not the registry value alone. So the old
    condition suppressed the prefix exactly when the process did not need it. Removing
    it is hardening rather than a bug fix: it makes the behaviour deterministic instead
    of dependent on a misleadingly named API, and the prefix is a no-op for a process
    that is already long path aware.

    :param original_path: Original unmodified path/string representing an absolute path.
    :return: A Path object representing the long path compatible path.
    """

    original_path_string = str(original_path)
    if sys.platform != "win32":
        return Path(original_path_string)

    if (
        len(original_path_string) + TEMP_DOWNLOAD_ADDED_CHARS_LENGTH >= WINDOWS_MAX_PATH_LENGTH
        and not original_path_string.startswith(WINDOWS_UNC_PATH_STRING_PREFIX)
    ):
        # A prefixed path is handed to the filesystem verbatim, with the Win32
        # normalization turned off, so it has to be fully normalized first.
        #
        # ntpath.normpath rather than PureWindowsPath: both convert separators and drop
        # "." components, but PureWindowsPath deliberately preserves ".." (it cannot
        # safely resolve them without touching the filesystem, since a component may be
        # a symlink). A surviving ".." is passed through literally once the prefix is
        # applied and the filesystem rejects it. normpath collapses it lexically, keeps
        # the leading pair of backslashes on \\server\share, and is idempotent on paths
        # that already carry the prefix.
        #
        # ntpath rather than os.path because this branch parses Windows paths on any
        # host: it is exercised on POSIX by tests that patch sys.platform, where
        # os.path is posixpath.
        normalized = ntpath.normpath(original_path_string)
        pure = PureWindowsPath(normalized)

        if pure.drive.startswith(WINDOWS_PATH_SEPARATOR * 2):
            # A network path (\\server\share) takes the \\?\UNC\ form, which replaces the
            # leading pair of backslashes with the prefix. Prepending \\?\ verbatim would
            # produce \\?\\\server\share, which Windows rejects.
            return Path(WINDOWS_UNC_DEVICE_PATH_STRING_PREFIX + normalized[2:])

        return Path(WINDOWS_UNC_PATH_STRING_PREFIX + normalized)
    return Path(original_path_string)


def _retry(
    ExceptionToCheck: Union[Type[Exception], Tuple[Type[Exception], ...]] = AssertionError,
    tries: int = 2,
    delay: Union[int, float, Tuple[Union[int, float], Union[int, float]]] = 1.0,
    backoff: float = 1.0,
    logger: Optional[Callable] = print,
) -> Callable:
    """Retry calling the decorated function using an exponential backoff.

    http://www.saltycrane.com/blog/2009/11/trying-out-retry-decorator-python/
    original from: http://wiki.python.org/moin/PythonDecoratorLibrary#Retry

    :param ExceptionToCheck: the exception to check. may be a tuple of
        exceptions to check
    :type ExceptionToCheck: Exception or tuple
    :param tries: number of times to try (not retry) before giving up
    :type tries: int
    :param delay: initial delay between retries in seconds
    :type delay: float or tuple
    :param backoff: backoff multiplier e.g. value of 2 will double the delay
        each retry
    :type backoff: float
    :param logger: logging function to use. If None, won't log
    :type logger: logging.Logger instance
    """

    def deco_retry(f: Callable) -> Callable:
        @wraps(f)
        def f_retry(*args: Any, **kwargs: Any) -> Callable:
            mtries: int = tries
            if isinstance(delay, (float, int)):
                mdelay = delay
            elif isinstance(delay, tuple):
                mdelay = random.uniform(delay[0], delay[1])
            else:
                raise ValueError(f"Provided delay {delay} isn't supported")

            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:
                    if logger:
                        logger(f"{str(e)}, Retrying in {mdelay} seconds...")
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        return f_retry  # true decorator

    return deco_retry
