"""Safe deletion of orphan files."""
from __future__ import annotations

import sys
from typing import Iterable

from snakeprune.walker import OrphanFile


def delete_orphans(orphans: Iterable[OrphanFile], allow_symlinks: bool = False) -> None:
    """Unlink each orphan file. Refuses to operate on directories. Refuses symlinks
    unless `allow_symlinks=True`. Prints what's being deleted to stderr.
    """
    for orphan in orphans:
        path = orphan.path
        if path.is_dir() and not path.is_symlink():
            raise IsADirectoryError(f"Refusing to delete directory: {path}")
        if path.is_symlink() and not allow_symlinks:
            raise PermissionError(
                f"Refusing to delete symlink {path} without --allow-symlinks"
            )
        print(f"deleting: {path}", file=sys.stderr)
        path.unlink()
