"""Build regexes from Snakemake rule path patterns."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

# Mirrors snakemake.io.WILDCARD_REGEX: a wildcard name, optionally followed by
# a comma and a constraint regex whose body may contain one level of nested
# `{n}` / `{n,m}` quantifier braces (e.g. `{chrom,[0-9]{1,2}}`). Possessive
# quantifiers (Python >= 3.11) keep malformed input from backtracking
# exponentially.
_WILDCARD_RE = re.compile(
    r"\{\s*(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?:\s*,\s*(?P<constraint>(?:[^{}]++|\{\d+(?:,\d+)?\})*+))?\s*\}"
)

DEFAULT_WILDCARD_REGEX = r"[^/]+"
SNAKEMAKE_WILDCARD_REGEX = r".+"


class PatternError(ValueError):
    """Raised when a rule path contains brace syntax the parser cannot read.

    Failing loudly here is a safety property: an unparsed `{...}` escaped into
    a literal would match nothing, silently turning every file of that rule
    into an orphan.
    """


def wildcard_pattern_to_regex(
    pattern: str,
    constraints: dict[str, str],
    default: str = DEFAULT_WILDCARD_REGEX,
    is_directory: bool = False,
) -> str:
    """Convert a Snakemake path pattern to an anchored regex string.

    Each `{wildcard}` or `{wildcard,regex}` placeholder on its first occurrence
    becomes a named capture group. The group body is, in order of precedence:
    the inline `regex` annotation, `constraints[wildcard]`, then `default`.
    This matches Snakemake (inline > rule-local > global). Subsequent
    occurrences of the same wildcard become backreferences `(?P=name)`,
    preserving Snakemake's implicit-equality semantics. All other characters
    are escaped for literal matching.

    `is_directory=True` appends `(?:/.*)?` so that files *inside* a
    `directory()` output are matched as live.

    Raises `PatternError` if any `{` or `}` survives in a literal segment.
    """
    parts: list[str] = []
    cursor = 0
    seen: set[str] = set()

    def _literal(text: str) -> str:
        if "{" in text or "}" in text:
            raise PatternError(
                f"Cannot parse wildcard syntax in rule path `{pattern}` "
                f"(unexpected brace in `{text}`). snakeprune refuses to "
                f"guess: an unparsed pattern would mark every file of this "
                f"rule as an orphan."
            )
        return re.escape(text)

    for match in _WILDCARD_RE.finditer(pattern):
        parts.append(_literal(pattern[cursor : match.start()]))
        name = match.group("name")
        if name in seen:
            parts.append(f"(?P={name})")
        else:
            inline = match.group("constraint")
            body = inline if inline else constraints.get(name, default)
            parts.append(f"(?P<{name}>{body})")
            seen.add(name)
        cursor = match.end()
    parts.append(_literal(pattern[cursor:]))
    suffix = r"(?:/.*)?" if is_directory else ""
    return "^" + "".join(parts) + suffix + "$"


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


def resolve_workdir(pipeline_dir: Path) -> Path:
    """Working directory Snakemake would use for `pipeline_dir`'s Snakefile.

    Mirrors `_extract._resolve_workdir`: the parent of the Snakefile's
    directory if that directory is named `workflow`, else the Snakefile's
    directory. Raises `SnakefileNotFound` if there is no Snakefile.
    """
    parent = resolve_snakefile(pipeline_dir).resolve().parent
    return parent.parent if parent.name == "workflow" else parent


def _split_outside_wildcards(path: str) -> list[str]:
    """Split `path` on `/`, never inside a `{name,constraint}` wildcard token."""
    segments: list[str] = []
    cursor = 0
    start = 0
    for match in _WILDCARD_RE.finditer(path):
        for i in range(cursor, match.start()):
            if path[i] == "/":
                segments.append(path[start:i])
                start = i + 1
        cursor = match.end()
    for i in range(cursor, len(path)):
        if path[i] == "/":
            segments.append(path[start:i])
            start = i + 1
    segments.append(path[start:])
    return segments


def normalise_rule_path(path: str) -> tuple[str, bool]:
    """Normalise `./`, `//`, a trailing `/` and collapsible `..` in a rule path.

    Returns `(normalised, has_dotdot)`. A `seg/..` pair collapses only when
    `seg` contains no wildcard (its value is unknown, so what it names is too)
    and is not itself `..`; every other `..` stays verbatim and sets the flag.
    """
    absolute = path.startswith("/")
    stack: list[str] = []
    for seg in _split_outside_wildcards(path):
        if seg in ("", "."):
            continue
        if (
            seg == ".."
            and stack
            and stack[-1] != ".."
            and not _WILDCARD_RE.search(stack[-1])
        ):
            stack.pop()
        else:
            stack.append(seg)
    return ("/" if absolute else "") + "/".join(stack), ".." in stack


from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuleSpec:
    """Raw rule path information extracted from a loaded workflow.

    Fields:
        name: the rule name as declared in the Snakefile.
        outputs: raw path strings for everything the rule produces (``output``,
            ``log`` and ``benchmark``), exactly as Snakemake stores them —
            inline ``{name,regex}`` constraint annotations are preserved.
        constraints: fallback wildcard constraints (rule-local merged over
            workflow-global) for wildcards with no inline annotation.
        directories: the subset of ``outputs`` declared with ``directory()``.
    """
    name: str
    outputs: list[str]
    constraints: dict[str, str]
    directories: list[str] = field(default_factory=list)


class ExtractorError(RuntimeError):
    """Raised when the rule-extractor subprocess fails or produces invalid output.

    The user-facing message is ``str(exc)``; the CLI translates this into
    exit code 4 with the message on stderr.
    """


def _extract_script_path() -> Path:
    """Resolve the absolute path of the `_extract.py` script shipped with this package."""
    return Path(__file__).parent / "_extract.py"


def load_rule_specs(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
) -> list[RuleSpec]:
    """Load rule output specs by running the standalone extractor in a subprocess.

    The signature is preserved from the in-process implementation so existing
    third-party callers and the project's own test suite keep working.
    """
    return run_extractor(pipeline_dir, configfiles=configfiles)


def run_extractor(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    *,
    _python_exe_for_testing: Path | None = None,
    _script_path_for_testing: Path | None = None,
) -> list[RuleSpec]:
    """Invoke the standalone extractor in a subprocess and return RuleSpec objects.

    The two `_*_for_testing` kwargs are private test seams. They override
    the `python` discovery and the script-path resolution respectively, so
    error-path tests can substitute a stub script or a deliberately-missing
    interpreter without touching the real environment.
    """
    if _python_exe_for_testing is not None:
        python_exe = Path(_python_exe_for_testing)
    else:
        found = shutil.which("python")
        if found is None:
            raise ExtractorError(
                "Python interpreter `python` not found on PATH. Activate "
                "your workflow environment where you would normally run "
                "`snakemake`."
            )
        python_exe = Path(found)

    script_path = (
        Path(_script_path_for_testing)
        if _script_path_for_testing is not None
        else _extract_script_path()
    )

    cmd: list[str] = [str(python_exe), str(script_path), str(pipeline_dir)]
    for cf in configfiles:
        cmd.extend(["--configfile", str(cf)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        if "No module named 'snakemake'" in result.stderr:
            raise ExtractorError(
                f"The extractor failed: snakemake is not importable in "
                f"`{python_exe}`. Activate the env where you'd normally run "
                f"`snakemake`."
            )
        raise ExtractorError(
            f"Extractor failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractorError(
            "Extractor produced unparseable output on stdout. "
            f"stderr was:\n{result.stderr.strip()}"
        ) from exc

    return [
        RuleSpec(
            name=r["name"],
            outputs=list(r["outputs"]),
            constraints=dict(r["constraints"]),
            directories=list(r.get("directories", [])),
        )
        for r in payload["rules"]
    ]


def results_dir_name(results_dir: Path) -> str:
    """Directory name of `results_dir`, even when spelled `.`, `..` or `x/..`.

    `Path.name` is empty for `.` and `..` for a path ending in `..`; those
    would defeat name-based guards and let a trash target climb out of the
    trash directory, so they are resolved to the real directory's name.
    """
    name = results_dir.name
    if name in ("", ".", ".."):
        name = Path(os.path.realpath(results_dir)).name
    return name


def results_match_prefix(pipeline_dir: Path, results_dir: Path) -> str:
    """Prefix under which scanned files are matched against rule patterns.

    The results dir's path relative to the workflow workdir, plus `/` (empty
    if they are the same directory). Both `abspath` and `realpath` forms of
    each directory are paired, so symlinks on either side still match. Falls
    back to `<results_dir.name>/` when the results dir is not under the workdir.
    """
    snakefile_dir = resolve_snakefile(pipeline_dir).parent
    workdirs = [resolve_workdir(pipeline_dir)]
    unresolved = snakefile_dir.parent if snakefile_dir.name == "workflow" else snakefile_dir
    workdirs.append(Path(os.path.abspath(unresolved)))
    results = [Path(os.path.abspath(results_dir)), Path(os.path.realpath(results_dir))]
    for wd in workdirs:
        for rd in results:
            if rd == wd:
                return ""
            if wd in rd.parents:
                return rd.relative_to(wd).as_posix() + "/"
    return results_dir_name(results_dir) + "/"


def _rewrite_under_results_dir(
    path: str, results_dir: Path | None, match_prefix: str | None = None
) -> str | None:
    """Rewrite an absolute `path` under `results_dir` to `<match_prefix><rel>`, else None.

    The path is tried as written and with the directory part of its literal
    prefix (everything before the first wildcard) symlink-resolved, so a rule
    path spelled through a symlink (`/tmp` vs `/private/tmp`, a symlinked
    `/scratch`) is still recognised as lying under the results dir.
    """
    if results_dir is None:
        return None
    if match_prefix is None:
        match_prefix = results_dir_name(results_dir) + "/"
    cut = path.find("{")
    literal = path if cut == -1 else path[:cut]
    literal_dir = literal[: literal.rfind("/")]
    resolved = os.path.realpath(literal_dir or "/").rstrip("/") + path[len(literal_dir) :]
    bases = (os.path.abspath(results_dir), os.path.realpath(results_dir))
    for candidate in dict.fromkeys((path, resolved)):
        for base in bases:
            if candidate.startswith(base + "/"):
                return match_prefix + candidate[len(base) + 1 :]
    return None


def find_rule_patterns(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    default_wildcard_regex: str = DEFAULT_WILDCARD_REGEX,
    on_warning: Callable[[str], None] | None = None,
    results_dir: Path | None = None,
    match_prefix: str | None = None,
) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per produced path.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern. `default_wildcard_regex` is the body used for
    wildcards with no constraint at all (default `[^/]+`; pass
    `SNAKEMAKE_WILDCARD_REGEX` for Snakemake's own `.+`).

    Absolute paths the extractor could not relativise against the workdir are
    handled in one of two ways. If `results_dir` is given and the path lies
    under it (by `abspath` or `realpath`), it is rewritten to
    `<match_prefix><rel>` (`match_prefix` defaults to `<results-dir-name>/`)
    and used normally. Otherwise it can never match
    a scanned file; it is skipped and reported through `on_warning` (if given)
    so the user knows the live set is narrower than the workflow.
    """
    # Pre-check: surface SnakefileNotFound before spawning the extractor
    # subprocess, so a missing Snakefile exits 2 (not 4 via ExtractorError).
    resolve_snakefile(pipeline_dir)
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            is_directory = output_str in spec.directories
            raw_path = output_str
            output_str, dotdot = normalise_rule_path(output_str)
            if dotdot and output_str.startswith("../"):
                output_str, dotdot = normalise_rule_path(
                    str(resolve_workdir(pipeline_dir)) + "/" + output_str
                )
            if dotdot:
                raise PatternError(
                    f"Cannot resolve `..` in rule `{spec.name}` path "
                    f"`{raw_path}`. snakeprune refuses to guess: a wrong "
                    f"pattern would mark every file of this rule as an orphan."
                )
            if output_str.startswith("/"):
                rewritten = _rewrite_under_results_dir(
                    output_str, results_dir, match_prefix
                )
                if rewritten is None:
                    if on_warning is not None:
                        on_warning(
                            f"WARNING: dropping absolute path `{raw_path}` "
                            f"from rule `{spec.name}`: it does not lie under "
                            f"the scanned results directory, so no scanned "
                            f"file can match it."
                        )
                    continue
                output_str = rewritten
            regex_str = wildcard_pattern_to_regex(
                output_str,
                spec.constraints,
                default=default_wildcard_regex,
                is_directory=is_directory,
            )
            try:
                compiled = re.compile(regex_str)
            except re.error as exc:
                raise PatternError(
                    f"Invalid wildcard constraint regex in rule "
                    f"`{spec.name}` path `{output_str}`: {exc}"
                ) from exc
            out.append((spec.name, compiled))
    return out


def extract_literal_prefix(compiled: re.Pattern) -> str:
    """Return the un-escaped literal prefix of an anchored rule regex.

    Reads ``compiled.pattern`` up to the first unescaped ``(`` (the first capture group)
    and reverses ``re.escape``-style backslash escaping so the result is a
    real path prefix. Returns the entire literal body (minus the trailing
    ``$``) if the pattern has no capture group.
    """
    body = compiled.pattern.lstrip("^")
    if body.endswith("$"):
        body = body[:-1]
    cut = -1
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "(":
            cut = i
            break
        i += 1
    literal = body if cut == -1 else body[:cut]
    return re.sub(r"\\(.)", r"\1", literal)


_NAMED_GROUP_OPEN_RE = re.compile(r"\(\?P<([A-Za-z_][A-Za-z_0-9]*)>")
_NAMED_BACKREF_RE = re.compile(r"\(\?P=([A-Za-z_][A-Za-z_0-9]*)\)")


# Over-inclusive on purpose: numeric backreferences (``\2``) and conditional
# groups (``(?(1)...)``) depend on group numbering, which merging changes.
_NUMBERED_GROUP_RE = re.compile(r"\\\d|\(\?\(")


class CombinedPattern:
    """Combined alternation plus patterns that cannot be merged safely."""

    def __init__(
        self, combined: re.Pattern | None, separate: list[re.Pattern]
    ) -> None:
        self._combined = combined
        self._separate = separate

    def match(self, target: str) -> bool:
        if self._combined is not None and self._combined.match(target):
            return True
        return any(p.match(target) for p in self._separate)


def combine_rule_patterns(
    patterns: list[tuple[str, re.Pattern]],
) -> CombinedPattern | None:
    """Combine per-rule output regexes into a single anchored alternation.

    Returns ``None`` if ``patterns`` is empty.

    Different rules typically reuse wildcard names (``{sample}`` everywhere),
    so a naive ``|``-join would fail with duplicate group names. To avoid that
    and to keep total capturing groups under Python's per-regex limit, each
    pattern is rewritten so:

    * named groups whose name is NOT referenced by ``(?P=name)`` in the same
      pattern are converted to non-capturing ``(?:...)``;
    * named groups whose name IS referenced (Snakemake's implicit equality
      between repeated wildcards) are renamed with a per-pattern prefix and
      their backreferences updated to match.

    Patterns using numeric backreferences or conditional groups are set aside
    and matched individually, since merging renumbers capture groups.

    The match semantics are unchanged from running each input regex separately.
    """
    if not patterns:
        return None
    parts: list[str] = []
    separate: list[re.Pattern] = []
    for i, (_, compiled) in enumerate(patterns):
        body = compiled.pattern
        if _NUMBERED_GROUP_RE.search(body):
            separate.append(compiled)
            continue
        if body.startswith("^"):
            body = body[1:]
        if body.endswith("$"):
            body = body[:-1]
        prefix = f"r{i}_"
        backref_names = set(_NAMED_BACKREF_RE.findall(body))

        def _replace_named(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in backref_names:
                return f"(?P<{prefix}{name}>"
            return "(?:"

        body = _NAMED_GROUP_OPEN_RE.sub(_replace_named, body)
        body = _NAMED_BACKREF_RE.sub(
            lambda m: f"(?P={prefix}{m.group(1)})", body
        )
        parts.append(f"(?:{body})")
    combined = None
    if parts:
        try:
            combined = re.compile("^(?:" + "|".join(parts) + ")$")
        except re.error as exc:
            raise PatternError(f"cannot combine rule patterns: {exc}") from exc
    return CombinedPattern(combined, separate)


def rule_literal_prefixes(patterns: list[tuple[str, re.Pattern]]) -> list[tuple[str, str]]:
    """Return ``(rule name, literal prefix)`` for each pattern, in order."""
    return [(name, extract_literal_prefix(regex)) for name, regex in patterns]
