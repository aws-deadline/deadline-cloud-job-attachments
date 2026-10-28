# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for accessing the local 'last seen on S3' cache.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .cache_db import CacheDB


logger = logging.getLogger("Deadline")


@dataclass
class S3CheckCacheEntry:
    """Represents an entry in the local s3 check cache database"""

    s3_key: str
    last_seen_time: str
    expiry_time: Optional[str] = None
    """
    POSIX timestamp after which the entry is no longer trusted, for example because an S3 Lifecycle
    rule will expire the object. Entries are never trusted for longer than ENTRY_EXPIRY_DAYS after
    last_seen_time, so None means that only ENTRY_EXPIRY_DAYS applies.
    """

    def to_dict(self) -> Dict[str, Any]:
        return {
            "s3_key": self.s3_key,
            "last_seen_time": self.last_seen_time,
            "expiry_time": self.expiry_time,
        }


class S3CheckCache(CacheDB):
    """
    Maintains a cache of 'last seen on S3' entries in a local database, which
    specifies which full S3 object keys exist in the content-addressed storage
    in the Job Attachments S3 bucket.

    This class is intended to always be used with a context manager to properly
    close the connection to the hash cache database.

    This class also automatically locks when doing writes, so it can be called
    by multiple threads.
    """

    CACHE_NAME = "s3_check_cache"
    # Version 2 added the expiry_time column. Each version uses its own table in the same database
    # file, so older library versions that share the file keep reading and writing their own table.
    CACHE_DB_VERSION = 2
    ENTRY_EXPIRY_DAYS = 30

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        table_name: str = f"s3checkV{self.CACHE_DB_VERSION}"
        create_query: str = (
            f"CREATE TABLE {table_name}"
            "(s3_key text primary key, last_seen_time timestamp, expiry_time timestamp)"
        )
        super().__init__(
            cache_name=self.CACHE_NAME,
            table_name=table_name,
            create_query=create_query,
            cache_dir=cache_dir,
        )

    @classmethod
    def compute_expiry_time(
        cls, last_seen_time: str, object_expiry: Optional[datetime] = None
    ) -> str:
        """
        Returns the expiry_time to store for an entry last seen at last_seen_time: the earlier of
        object_expiry (when the entry should stop being trusted because of what S3 will do to the
        object), and ENTRY_EXPIRY_DAYS after last_seen_time.
        """
        expiry = float(last_seen_time) + timedelta(days=cls.ENTRY_EXPIRY_DAYS).total_seconds()
        if object_expiry is not None:
            expiry = min(expiry, object_expiry.timestamp())
        return str(expiry)

    def get_connection_entry(self, s3_key: str, connection) -> Optional[S3CheckCacheEntry]:
        """
        Returns an entry from the hash cache, if it exists.  This is the "lockless" (Doesn't take
        the main db_lock protecting db_connection) version of get_entry which expects a connection
        parameter for the connection which will be used to read from the DB - this can generally
        be the thread local connection returned by get_local_connection()
        """

        entry_vals = connection.execute(
            f"SELECT s3_key, last_seen_time, expiry_time FROM {self.table_name} WHERE s3_key=?",
            [s3_key],
        ).fetchone()
        if entry_vals:
            entry = S3CheckCacheEntry(
                s3_key=entry_vals[0],
                last_seen_time=str(entry_vals[1]),
                expiry_time=None if entry_vals[2] is None else str(entry_vals[2]),
            )
            try:
                now = datetime.now()
                last_seen = datetime.fromtimestamp(float(entry.last_seen_time))
                if (now - last_seen).days >= self.ENTRY_EXPIRY_DAYS:
                    return None
                if entry.expiry_time is not None and now.timestamp() >= float(entry.expiry_time):
                    return None
                return entry
            except (ValueError, OverflowError, OSError):
                logger.warning(f"Timestamp for S3 key {s3_key} is not valid. Ignoring.")

        return None

    def get_entry(self, s3_key: str) -> Optional[S3CheckCacheEntry]:
        """
        Checks if an entry exists in the cache, and returns it if it hasn't expired.
        """
        if not self.enabled:
            return None

        with self.db_lock, self.db_connection:
            return self.get_connection_entry(s3_key, self.db_connection)

    def put_entry(self, entry: S3CheckCacheEntry) -> None:
        """Inserts or replaces an entry into the cache database."""
        if self.enabled:
            with self.db_lock, self.db_connection:
                self.db_connection.execute(
                    f"INSERT OR REPLACE INTO {self.table_name}(s3_key, last_seen_time, expiry_time)"
                    " VALUES(:s3_key, :last_seen_time, :expiry_time)",
                    entry.to_dict(),
                )
