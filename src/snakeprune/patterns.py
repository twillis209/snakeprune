"""Build regexes from Snakemake rule output patterns."""
from __future__ import annotations

import re

_WILDCARD_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")


def wildcard_pattern_to_regex(pattern: str, constraints: dict[str, str]) -> str:
    """Convert a Snakemake output pattern to an anchored regex string.

    Each {wildcard} placeholder on its first occurrence is replaced with a named
    capture group whose body is taken from `constraints[wildcard]` if present,
    else the default `[^/]+` (matching Snakemake's own default). Subsequent
    occurrences of the same wildcard in the pattern become backreferences
    (?P=name), preserving Snakemake's implicit-equality semantics for repeated
    wildcards. All other characters in the pattern are escaped for literal regex
    matching.
    """
    parts: list[str] = []
    cursor = 0
    seen: set[str] = set()
    for match in _WILDCARD_RE.finditer(pattern):
        literal = pattern[cursor : match.start()]
        parts.append(re.escape(literal))
        name = match.group(1)
        if name in seen:
            parts.append(f"(?P={name})")
        else:
            body = constraints.get(name, r"[^/]+")
            parts.append(f"(?P<{name}>{body})")
            seen.add(name)
        cursor = match.end()
    parts.append(re.escape(pattern[cursor:]))
    return "^" + "".join(parts) + "$"


from pathlib import Path


class SnakefileNotFound(FileNotFoundError):
    """Raised when no Snakefile exists at either standard location."""


def resolve_snakefile(pipeline_dir: Path) -> Path:
    """Find the Snakefile in `pipeline_dir`.

    Checks `<pipeline_dir>/Snakefile` first, then `<pipeline_dir>/workflow/Snakefile`
    (Snakemake's recommended layout). Raises `SnakefileNotFound` if neither exists.
    """
    candidates = [pipeline_dir / "Snakefile", pipeline_dir / "workflow" / "Snakefile"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SnakefileNotFound(
        "No Snakefile found at either:\n"
        f"  {candidates[0]}\n"
        f"  {candidates[1]}\n"
        "Pass --pipeline-dir pointing at the directory containing your Snakefile."
    )
