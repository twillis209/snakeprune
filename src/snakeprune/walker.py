"""Walk a results directory and apply ignore/symlink filters."""
from __future__ import annotations

import os
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
    exclude_dirs: Iterable[str] = (),
    stats: dict | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(full_path, rel_posix)`` string pairs for regular files under
    ``results_dir``, skipping ignored paths and (by default) symlinks.

    Strings rather than ``Path`` objects: the per-file path-string slicing here
    is much cheaper than constructing a ``Path`` (and calling ``relative_to`` /
    ``as_posix`` on it) in the consumer's hot loop. Callers that need a
    ``Path`` (e.g. to record an orphan) construct one on demand from the
    yielded full-path string.

    Uses ``os.scandir`` for traversal so each entry's type can be read from the
    cached dirent on filesystems that support ``d_type``, avoiding the extra
    ``stat`` syscalls that ``Path.rglob`` + ``Path.is_file/is_symlink`` incur.

    ``exclude_dirs`` is an iterable of directory path strings (absolute or
    relative; normalised internally with ``os.path.abspath``) whose subtrees
    are pruned entirely from the walk — the directory is never descended into.

    If ``stats`` is provided, the walker initialises
    ``stats["skipped_symlinked_dirs"] = 0`` and increments it once per
    directory entry that is a symlink to a directory and is being skipped
    because ``follow_symlinks=False``. It likewise initialises
    ``stats["excluded_dirs"] = 0`` and increments it once per directory
    pruned by ``exclude_dirs``.
    """
    ignore_globs = tuple(ignore_globs)
    exclude_set = {os.path.abspath(p) for p in exclude_dirs}
    if stats is not None:
        stats["skipped_symlinked_dirs"] = 0
        stats["excluded_dirs"] = 0
    base_str = os.fspath(results_dir)
    base_len = len(base_str) + 1  # length of "<base>/" prefix to strip
    sep = os.sep
    needs_sep_swap = sep != "/"

    stack: list[str] = [base_str]
    while stack:
        current = stack.pop()
        try:
            scandir_it = os.scandir(current)
        except OSError:
            continue
        with scandir_it:
            for entry in scandir_it:
                try:
                    is_link = entry.is_symlink()
                except OSError:
                    continue
                if is_link and not follow_symlinks:
                    if stats is not None:
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                stats["skipped_symlinked_dirs"] += 1
                        except OSError:
                            pass
                    continue
                try:
                    # Don't recurse into symlinked directories — preserves the
                    # prior `Path.rglob` default of not following dir symlinks
                    # even when `follow_symlinks=True` (which only governs
                    # whether symlinked *files* are yielded).
                    if not is_link and entry.is_dir(follow_symlinks=False):
                        if exclude_set and os.path.abspath(entry.path) in exclude_set:
                            if stats is not None:
                                stats["excluded_dirs"] += 1
                            continue
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                full_path = entry.path
                rel = full_path[base_len:]
                if needs_sep_swap:
                    rel = rel.replace(sep, "/")
                if ignore_globs and _matches_any_glob(rel, ignore_globs):
                    continue
                yield full_path, rel


from dataclasses import dataclass
import re

from snakeprune.patterns import (
    combine_rule_patterns,
    extract_literal_prefix,
    find_rule_patterns,
)


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    rel: str
    likely_rule: str | None = None


def attribute_orphan_to_rule(target: str, patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """Best-effort guess: the rule whose output pattern shares the longest literal
    prefix with `target`. Falls back to None if no rule shares a meaningful prefix.
    """
    best_rule: str | None = None
    best_prefix_len = 0
    for name, regex in patterns:
        prefix = extract_literal_prefix(regex)
        if target.startswith(prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_rule = name
    return best_rule


def find_orphans(
    pipeline_dir: Path,
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
    attribute_rules: bool = False,
) -> list[OrphanFile]:
    """Return regular files under `results_dir` that match no rule output pattern."""
    patterns = find_rule_patterns(pipeline_dir)
    combined = combine_rule_patterns(patterns)
    orphans: list[OrphanFile] = []
    target_prefix = results_dir.name + "/"
    for full_path, rel in iter_results_files(
        results_dir, ignore_globs=ignore_globs, follow_symlinks=follow_symlinks
    ):
        match_target = target_prefix + rel
        if combined is not None and combined.match(match_target):
            continue
        likely = attribute_orphan_to_rule(match_target, patterns) if attribute_rules else None
        orphans.append(OrphanFile(path=Path(full_path), rel=rel, likely_rule=likely))
    return orphans
