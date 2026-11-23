"""Walk a results directory and apply ignore/symlink filters."""
from __future__ import annotations

import itertools
import os
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Iterator


def _expand_globs(globs: Iterable[str]) -> tuple[str, ...]:
    """Each glob plus every variant with one or more ``**/`` occurrences removed,
    so that ``**/`` also matches zero directories (``**/*.bai`` -> ``*.bai``)."""
    out: dict[str, None] = {}
    for g in globs:
        parts = g.split("**/")
        # Each separator between parts is either kept ("**/") or dropped ("").
        for keep in itertools.product(("**/", ""), repeat=len(parts) - 1):
            out[parts[0] + "".join(k + p for k, p in zip(keep, parts[1:]))] = None
    return tuple(out)


def _matches_expanded(rel_path: str, expanded: Iterable[str]) -> bool:
    return any(fnmatchcase(rel_path, g) or _glob_matches_subdir(rel_path, g) for g in expanded)


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    return _matches_expanded(rel_path, _expand_globs(globs))


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
    directory entry that is a symlink to a directory; such entries are never
    descended into, whether or not ``follow_symlinks`` is set (which only
    governs whether symlinked *files* are yielded). It likewise initialises
    ``stats["excluded_dirs"] = 0`` and increments it once per directory
    pruned by ``exclude_dirs``.
    """
    ignore_globs = _expand_globs(ignore_globs)
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
                if is_link:
                    try:
                        is_dir_link = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        continue
                    if is_dir_link:
                        # Symlinked directories are never descended into, in
                        # either mode; count them so the CLI can tell the user.
                        if stats is not None:
                            stats["skipped_symlinked_dirs"] += 1
                        continue
                    if not follow_symlinks:
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
                if ignore_globs and _matches_expanded(rel, ignore_globs):
                    continue
                yield full_path, rel


from dataclasses import dataclass
import re

from snakeprune.patterns import extract_literal_prefix


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    rel: str
    likely_rule: str | None = None


def attribute_orphan_to_rule(
    target: str,
    patterns: list[tuple[str, re.Pattern]],
    prefixes: list[tuple[str, str]] | None = None,
) -> str | None:
    """Best-effort guess: the rule whose path pattern shares the longest literal
    prefix with `target`. Falls back to None if no rule shares a meaningful prefix.

    Pass precomputed `prefixes` (see `rule_literal_prefixes`) to avoid
    recomputing them per call; `patterns` is then not consulted.
    """
    if prefixes is None:
        prefixes = [(name, extract_literal_prefix(regex)) for name, regex in patterns]
    best_rule: str | None = None
    best_prefix_len = 0
    for name, prefix in prefixes:
        if target.startswith(prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_rule = name
    return best_rule

