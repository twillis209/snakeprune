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


from dataclasses import dataclass
import re

from snakeprune.patterns import find_rule_patterns


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    likely_rule: str | None = None


def _attribute_rule(target: str, patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """Best-effort guess: the rule whose output pattern shares the longest literal
    prefix with `target`. Falls back to None if no rule shares a meaningful prefix.
    """
    best_rule: str | None = None
    best_prefix_len = 0
    for name, regex in patterns:
        # Reconstruct the literal prefix by reading regex.pattern up to the first '('
        # (the first wildcard capture group). Anchored '^' is the first character.
        literal_prefix = ""
        body = regex.pattern.lstrip("^")
        for ch in body:
            if ch == "(":
                break
            literal_prefix += ch
        # Un-escape: in `re.escape`, '/' is left as-is, so the prefix is a real path prefix
        # except for '\\' before special characters. Strip backslashes that precede
        # ASCII non-alphanumeric chars to recover the source path.
        unescaped = re.sub(r"\\(.)", r"\1", literal_prefix)
        if target.startswith(unescaped) and len(unescaped) > best_prefix_len:
            best_prefix_len = len(unescaped)
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
    orphans: list[OrphanFile] = []
    for path in iter_results_files(results_dir, ignore_globs=ignore_globs, follow_symlinks=follow_symlinks):
        match_target = results_dir.name + "/" + path.relative_to(results_dir).as_posix()
        if any(p.match(match_target) for _, p in patterns):
            continue
        likely = _attribute_rule(match_target, patterns) if attribute_rules else None
        orphans.append(OrphanFile(path=path, likely_rule=likely))
    return orphans
