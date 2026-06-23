"""Safe deletion of orphan files."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Iterable

from snakeprune.walker import OrphanFile


def delete_orphans(
    orphans: Iterable[OrphanFile],
    allow_symlinks: bool = False,
    trash_dir: Path | None = None,
    results_dir_name: str | None = None,
) -> None:
    """Remove each orphan: either ``path.unlink()`` (default) or
    ``shutil.move`` into ``trash_dir / results_dir_name / orphan.rel`` when
    ``trash_dir`` is provided. Refuses to operate on directories. Refuses
    symlinks unless ``allow_symlinks=True``. Prints what's being removed to
    stderr.

    ``results_dir_name`` is required when ``trash_dir`` is provided so that a
    single trash dir can be reused across multiple results dirs without
    collisions.

    Validation is all-or-nothing: every orphan is checked against the directory
    and symlink guards *before* anything is removed, so a single disallowed
    entry aborts the whole batch without having destroyed the entries listed
    before it.
    """
    if trash_dir is not None and results_dir_name is None:
        raise ValueError("results_dir_name is required when trash_dir is set")
    orphans = list(orphans)
    for orphan in orphans:
        path = orphan.path
        if path.is_dir() and not path.is_symlink():
            raise IsADirectoryError(f"Refusing to delete directory: {path}")
        if path.is_symlink() and not allow_symlinks:
            raise PermissionError(
                f"Refusing to delete symlink {path} without --allow-symlinks"
            )
    for orphan in orphans:
        path = orphan.path
        if trash_dir is not None:
            assert results_dir_name is not None  # for type-checkers
            target = trash_dir / results_dir_name / orphan.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"moving to trash: {path} -> {target}", file=sys.stderr)
            shutil.move(str(path), str(target))
        else:
            print(f"deleting: {path}", file=sys.stderr)
            path.unlink()
