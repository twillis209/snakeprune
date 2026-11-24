"""The per-results-directory ignore file, ``.snakepruneignore``.

It records, in the tree itself, what snakeprune must never touch, so keeping
a snapshot directory does not depend on remembering a flag on every run.

Format, one entry per line, paths relative to the results directory (and
anchored there, unlike ``.gitignore``):

* blank lines and lines starting with ``#`` are skipped;
* ``name/`` (no glob characters) protects that directory subtree; it must
  exist, exactly as ``--exclude-dir`` requires, so a typo is refused rather
  than silently protecting nothing;
* ``pattern/`` with glob characters (e.g. ``_*/``) protects every file under
  any directory the pattern matches;
* anything else is a glob with ``--ignore`` semantics (``*`` crosses ``/``,
  ``prefix/**`` covers a subtree).

``!`` negation is not supported and is refused, since guessing what it means
could unprotect files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

IGNORE_FILE_NAME = ".snakepruneignore"
_GLOB_CHARS = set("*?[")


class IgnoreFileError(ValueError):
    """The ignore file cannot be read or contains an entry snakeprune refuses."""


@dataclass(frozen=True)
class IgnoreEntry:
    line: int
    text: str  # the entry as written, for messages


@dataclass
class IgnoreFile:
    path: Path
    # (entry, glob) pairs: what the walker matches against.
    globs: list[tuple[IgnoreEntry, str]] = field(default_factory=list)
    # (entry, relative directory) pairs: subtrees pruned from the walk.
    dirs: list[tuple[IgnoreEntry, str]] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        return len(self.globs) + len(self.dirs)


def load_ignore_file(results_dir: Path) -> IgnoreFile | None:
    """Parse ``results_dir/.snakepruneignore``; None if there is no such file.

    Raises ``IgnoreFileError`` if it exists but is not a readable UTF-8
    regular file, or holds an entry snakeprune refuses to interpret.
    """
    path = results_dir / IGNORE_FILE_NAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise IgnoreFileError(f"{path} is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IgnoreFileError(f"cannot read {path}: {exc}") from exc

    result = IgnoreFile(path=path)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry = IgnoreEntry(lineno, line)
        where = f"{path}, line {lineno} (`{line}`)"
        if line.startswith("!"):
            raise IgnoreFileError(
                f"{where}: `!` negation is not supported; remove the line"
            )
        if line.startswith("/") or line.split("/")[0] == "..":
            raise IgnoreFileError(
                f"{where}: entries are relative to the results directory; "
                f"drop the leading `/` or `..`"
            )
        if line.endswith("/"):
            body = line.rstrip("/")
            if not body:
                raise IgnoreFileError(f"{where}: an entry cannot be just `/`")
            if _GLOB_CHARS & set(body):
                result.globs.append((entry, body + "/*"))
            else:
                result.dirs.append((entry, body))
        else:
            result.globs.append((entry, line))
    return result
