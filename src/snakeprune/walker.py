"""Walk a results directory and apply ignore/symlink filters."""
from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Iterator


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    return any(fnmatchcase(rel_path, g) or _glob_matches_subdir(rel_path, g) for g in globs)


def _glob_matches_subdir(rel_path: str, glob: str) -> bool:
    # Treat 'sub/**' as matching anything under 'sub/'
    if glob.endswith("/**"):
        prefix = glob[: -len("/**")]
        return rel_path == prefix or rel_path.startswith(prefix + "/")
    return False


def iter_results_files(
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Yield regular files under `results_dir`, skipping ignored paths and (by default) symlinks."""
    ignore_globs = tuple(ignore_globs)
    for path in results_dir.rglob("*"):
        if path.is_symlink() and not follow_symlinks:
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(results_dir).as_posix()
        if ignore_globs and _matches_any_glob(rel, ignore_globs):
            continue
        yield path
