"""Persistence layer for collected job postings.

``sqlite`` is the only backend today; :func:`open_storage` turns the
validated ``storage:`` config block into a ready :class:`Storage`.
"""

from __future__ import annotations

import sqlite3

from jobsearcher.config import BackendSectionConfig
from jobsearcher.storage.base import Storage
from jobsearcher.storage.sqlite import SqliteStorage

_DEFAULT_SQLITE_PATH = "./jobsearcher.db"


class StorageConfigError(Exception):
    """The ``storage:`` configuration names an unknown or unusable backend."""


def open_storage(config: BackendSectionConfig) -> SqliteStorage:
    """Open the storage backend named by ``config``.

    Args:
        config: The validated ``storage:`` block. Its ``backend`` selects the
            implementation; remaining keys are backend-specific (``sqlite``
            reads ``path``, defaulting to ``./jobsearcher.db``).

    Returns:
        An open storage handle. The caller owns it and should close it (it is
        a context manager).

    Raises:
        StorageConfigError: If ``backend`` is not ``sqlite``, or the
            database at ``path`` cannot be opened or prepared — a missing
            parent directory, a permission problem, a file that is not a
            SQLite database. Every command opens storage first, so these
            surface as an actionable message rather than a traceback. The
            two cases get different messages: only one of them is about the
            directory, and telling someone to check permissions they have
            not touched sends them looking in the wrong place.
    """
    if config.backend != "sqlite":
        raise StorageConfigError(
            f"unknown storage backend {config.backend!r}; only 'sqlite' is supported"
        )
    extra = config.model_dump()
    path = extra.get("path") or _DEFAULT_SQLITE_PATH
    try:
        return SqliteStorage(str(path))
    except sqlite3.OperationalError as exc:
        # SQLite could not get at the file at all: no such directory, no
        # permission, a lock it could not take.
        raise StorageConfigError(
            f"could not open the SQLite database at '{path}' (from storage.path): {exc}. "
            f"Check that its parent directory exists and is writable, and that no other "
            f"JobSearcher process is holding the file."
        ) from exc
    except sqlite3.Error as exc:
        # The file was reachable; reading or migrating it is what failed.
        raise StorageConfigError(
            f"the SQLite database at '{path}' (from storage.path) could not be prepared: "
            f"{exc}. The file was found and opened, so this is about its contents rather "
            f"than the directory: check that it is a JobSearcher database and not another "
            f"SQLite file, or move it aside and let JobSearcher create a new one."
        ) from exc


__all__ = ["SqliteStorage", "Storage", "StorageConfigError", "open_storage"]
