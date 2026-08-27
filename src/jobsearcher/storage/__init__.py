"""Persistence layer for collected job postings.

``sqlite`` is the only backend today; :func:`open_storage` turns the
validated ``storage:`` config block into a ready :class:`Storage`.
"""

from __future__ import annotations

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
        StorageConfigError: If ``backend`` is not ``sqlite``.
    """
    if config.backend != "sqlite":
        raise StorageConfigError(
            f"unknown storage backend {config.backend!r}; only 'sqlite' is supported"
        )
    extra = config.model_dump()
    path = extra.get("path") or _DEFAULT_SQLITE_PATH
    return SqliteStorage(str(path))


__all__ = ["SqliteStorage", "Storage", "StorageConfigError", "open_storage"]
