"""Persistence layer for collected job postings."""

from jobsearcher.storage.base import Storage
from jobsearcher.storage.sqlite import SqliteStorage

__all__ = ["SqliteStorage", "Storage"]
