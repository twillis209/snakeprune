"""Safe deletion of orphan files."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Iterable

from snakeprune.walker import OrphanFile


class DeletionFailed(RuntimeError):
    """Raised when a delete/trash batch fails part-way through execution.

    Validation is all-or-nothing, but the execution loop can still hit an
    ``OSError`` (permissions changed, disk full, file vanished). The exception
    records how far the batch got so the CLI can tell the user exactly what
    is already gone.
    """

    def __init__(self, completed: int, total: int, path: Path, cause: OSError):
        self.completed = completed
        self.total = total
        self.path = path
        super().__init__(
            f"Deleted {completed} of {total} file(s) before failing at "
            f"{path}: {cause}. The files listed before it are already gone; "
            f"the rest were not touched."
        )


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

    Validation is all-or-nothing: every orphan is checked against the directory,
    symlink, and (in trash mode) collision guards *before* anything is moved or
    removed, so a single disallowed entry aborts the whole batch without having
    destroyed the entries listed before it. If an ``OSError`` occurs during
    execution, ``DeletionFailed`` is raised with the count of entries already
    removed.
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
        if trash_dir is not None:
            assert results_dir_name is not None  # for type-checkers
            target = trash_dir / results_dir_name / orphan.rel
            if target.exists() or target.is_symlink():
                raise FileExistsError(
                    f"Refusing to move {path} to trash: {target} already "
                    f"exists. Empty or rotate the trash directory, or choose "
                    f"another."
                )
    total = len(orphans)
    for completed, orphan in enumerate(orphans):
        path = orphan.path
        try:
            if trash_dir is not None:
                assert results_dir_name is not None  # for type-checkers
                target = trash_dir / results_dir_name / orphan.rel
                target.parent.mkdir(parents=True, exist_ok=True)
                print(f"moving to trash: {path} -> {target}", file=sys.stderr)
                shutil.move(str(path), str(target))
            else:
                print(f"deleting: {path}", file=sys.stderr)
                path.unlink()
        except OSError as exc:
            raise DeletionFailed(completed, total, path, exc) from exc
