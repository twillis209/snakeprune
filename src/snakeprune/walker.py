"""Walk a results directory and apply ignore/symlink filters."""
from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator


def nfc(text: str) -> str:
    """Unicode NFC form of ``text`` (some filesystems store names decomposed)."""
    return text if text.isascii() else unicodedata.normalize("NFC", text)


# Each `**/` multiplies the regex's worst-case backtracking by the path depth.
MAX_DOUBLE_STARS = 3


def _glob_regex(glob: str) -> str:
    """Regex for one glob: ``fnmatch`` rules (``*`` crosses ``/``), except
    that ``**/`` also matches zero directories (``**/*.bai`` covers ``x.bai``)."""
    parts = (re.sub(r"\\[Zz]$", "", fnmatch.translate(p)) for p in glob.split("**/"))
    return "(?s:.*/)?".join(parts)


def _compile_globs(globs: Iterable[str]) -> re.Pattern | None:
    """One regex matching any of ``globs``, or None if there are none.

    A glob ending in ``/**`` also matches its prefix itself. Globs are
    normalised to NFC; match them against NFC paths. Raises ``ValueError``
    for a glob with more than ``MAX_DOUBLE_STARS`` separate ``**/``.
    """
    alternatives: list[str] = []
    for glob in globs:
        # `**/**/` means the same as `**/`.
        glob = re.sub(r"(?:\*\*/){2,}", "**/", nfc(glob))
        if glob.count("**/") > MAX_DOUBLE_STARS:
            raise ValueError(
                f"--ignore glob `{glob}` uses `**/` more than "
                f"{MAX_DOUBLE_STARS} times, which would make matching very slow"
            )
        alternatives.append(_glob_regex(glob))
        if glob.endswith("/**"):
            alternatives.append(_glob_regex(glob[: -len("/**")]))
    if not alternatives:
        return None
    return re.compile("|".join(f"(?:{a})" for a in alternatives))


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    compiled = _compile_globs(globs)
    return compiled is not None and compiled.fullmatch(nfc(rel_path)) is not None


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
    ignore_re = _compile_globs(ignore_globs)
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
                if ignore_re is not None and ignore_re.fullmatch(nfc(rel)):
                    continue
                yield full_path, rel


from dataclasses import dataclass

from snakeprune.patterns import extract_literal_prefix


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    rel: str
    likely_rule: str | None = None
    # Matches a rule only if an unconstrained wildcard may span `/`.
    ambiguous: bool = False


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

