"""Build regexes from Snakemake rule output patterns."""
from __future__ import annotations

import re

_WILDCARD_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")


def wildcard_pattern_to_regex(pattern: str, constraints: dict[str, str]) -> str:
    """Convert a Snakemake output pattern to an anchored regex string.

    Each {wildcard} placeholder is replaced with a named capture group whose body
    is taken from `constraints[wildcard]` if present, else the default `[^/]+`
    (matching Snakemake's own default). All other characters in the pattern are
    escaped for literal regex matching.
    """
    parts: list[str] = []
    cursor = 0
    for match in _WILDCARD_RE.finditer(pattern):
        literal = pattern[cursor : match.start()]
        parts.append(re.escape(literal))
        name = match.group(1)
        body = constraints.get(name, r"[^/]+")
        parts.append(f"(?P<{name}>{body})")
        cursor = match.end()
    parts.append(re.escape(pattern[cursor:]))
    return "^" + "".join(parts) + "$"
