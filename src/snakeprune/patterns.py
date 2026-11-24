"""Build regexes from Snakemake rule path patterns."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
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

def nfc(text: str) -> str:
    """Unicode NFC form of `text` (some filesystems store names decomposed)."""
    return text if text.isascii() else unicodedata.normalize("NFC", text)


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
    overrides: dict[str, str] | None = None,
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

    `overrides[wildcard]`, when given, is used in place of all of the above
    (see `rule_wildcard_bodies`).

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
            if overrides is not None and name in overrides:
                body = overrides[name]
            else:
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
        inputs: static input path strings. A file a rule reads is live even
            if no rule produces it (a frozen source, say): deleting it would
            break the workflow.
        dynamic_inputs: how many input entries are functions, whose paths
            cannot be known without running the DAG; they protect nothing.
    """
    name: str
    outputs: list[str]
    constraints: dict[str, str]
    directories: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    dynamic_inputs: int = 0


class ExtractorError(RuntimeError):
    """Raised when the rule-extractor subprocess fails or produces invalid output.

    The user-facing message is ``str(exc)``; the CLI translates this into
    exit code 4 with the message on stderr.
    """


def _extract_script_path() -> Path:
    """Resolve the absolute path of the `_extract.py` script shipped with this package."""
    return Path(__file__).parent / "_extract.py"


@dataclass(frozen=True)
class ExtractedWorkflow:
    """Rule specs plus the working directory their paths are relative to.

    ``workdir_source`` says where the workdir came from: ``"default"`` (the
    Snakefile's directory, or its parent for ``workflow/Snakefile``),
    ``"directive"`` (the Snakefile's ``workdir:``), ``"option"``
    (``--directory``) or ``"chdir"`` (the Snakefile changed directory some
    other way while it was parsed). In every case it is the directory
    Snakemake ends up in after parsing, where it runs the jobs.
    """
    rules: list[RuleSpec]
    workdir: Path
    workdir_source: str = "default"


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
    """Invoke the extractor and return only its RuleSpec objects."""
    return extract_workflow(
        pipeline_dir,
        configfiles=configfiles,
        _python_exe_for_testing=_python_exe_for_testing,
        _script_path_for_testing=_script_path_for_testing,
    ).rules


def extract_workflow(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    directory: Path | None = None,
    *,
    _python_exe_for_testing: Path | None = None,
    _script_path_for_testing: Path | None = None,
) -> ExtractedWorkflow:
    """Invoke the standalone extractor in a subprocess.

    ``directory`` overrides the working directory, as ``snakemake -d`` does.

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
    if directory is not None:
        cmd.extend(["--directory", str(directory)])

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

    rules = [
        RuleSpec(
            name=r["name"],
            outputs=list(r["outputs"]),
            constraints=dict(r["constraints"]),
            directories=list(r.get("directories", [])),
            inputs=list(r.get("inputs", [])),
            dynamic_inputs=int(r.get("dynamic_inputs", 0)),
        )
        for r in payload["rules"]
    ]
    workdir = payload.get("workdir")
    return ExtractedWorkflow(
        rules=rules,
        workdir=Path(workdir) if workdir else resolve_workdir(pipeline_dir),
        workdir_source=payload.get("workdir_source", "default"),
    )


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


def results_match_prefix(
    pipeline_dir: Path, results_dir: Path, workdir: Path | None = None
) -> str:
    """Prefix under which scanned files are matched against rule patterns.

    The results dir's path relative to the workflow workdir, plus `/` (empty
    if they are the same directory). Both `abspath` and `realpath` forms of
    each directory are paired, so symlinks on either side still match. Falls
    back to `<results_dir.name>/` when the results dir is not under the workdir.

    `workdir` overrides the default workdir (a `workdir:` directive or
    `--directory`).
    """
    if workdir is not None:
        workdirs = [Path(os.path.realpath(workdir)), Path(os.path.abspath(workdir))]
    else:
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
                return nfc(rd.relative_to(wd).as_posix()) + "/"
    return nfc(results_dir_name(results_dir)) + "/"


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
        match_prefix = nfc(results_dir_name(results_dir)) + "/"
    cut = path.find("{")
    literal = path if cut == -1 else path[:cut]
    literal_dir = literal[: literal.rfind("/")]
    resolved = os.path.realpath(literal_dir or "/").rstrip("/") + path[len(literal_dir) :]
    # Compared in NFC: the rule text and the on-disk spelling may differ in form.
    # A spelling that differs only in letter case counts only where the
    # filesystem agrees it is the same directory (case-insensitive ones,
    # such as macOS's default): elsewhere `/data/Results` is a different
    # directory, and mapping it would let the "no rule writes here" guard
    # pass on a path no rule writes to.
    bases = (nfc(os.path.abspath(results_dir)), nfc(os.path.realpath(results_dir)))
    for candidate in dict.fromkeys((nfc(path), nfc(resolved))):
        for base in bases:
            head = candidate[: len(base) + 1]
            if head == base + "/":
                return match_prefix + candidate[len(base) + 1 :]
            if head.casefold() == (base + "/").casefold():
                try:
                    same = os.path.samefile(head[:-1], base)
                except OSError:
                    same = False
                if same:
                    return match_prefix + candidate[len(base) + 1 :]
    return None


def _literal_head(path: str) -> tuple[str, bool]:
    """Text of `path` before its first brace, and whether it had one."""
    cut = min((i for i in (path.find("{"), path.find("}")) if i != -1), default=-1)
    return (path, False) if cut == -1 else (path[:cut], True)


def _may_lie_in_results(
    path: str,
    results_dir: Path | None,
    match_prefix: str | None,
    contains: bool,
) -> bool:
    """Whether a relative rule path snakeprune cannot match may name a
    scanned file.

    Relative paths are relative to the workdir, and scanned files are
    matched under `match_prefix` and `<results-dir-name>/`. Decided from the
    literal text before the first brace, ignoring case, erring towards
    "may": the text lies under one of those prefixes, or a wildcard could
    lead into one, or (`contains`, for a path that may be a directory) it
    names the working directory or an ancestor of the results tree.
    Without a `results_dir` nothing is known, and False is returned.
    """
    if results_dir is None:
        return False
    head, wild = _literal_head(nfc(path))
    if match_prefix is None:
        match_prefix = nfc(results_dir_name(results_dir)) + "/"
    h = head.casefold()
    for base in {match_prefix, nfc(results_dir_name(results_dir)) + "/"}:
        base = base.casefold()
        if h.startswith(base):
            return True  # inside the results tree
        if wild and base.startswith(h):
            return True  # a wildcard could lead into it
        if not wild and contains and (h in ("", ".") or base.startswith(h.rstrip("/") + "/")):
            return True  # the working directory, the results dir or an ancestor
    return False


WILDCARD_ABSOLUTE_FLAG = "--allow-wildcard-absolute-paths"


def _wildcard_in_directory_part(path: str) -> bool:
    """Whether any component of `path` but the last holds a brace."""
    return any("{" in seg or "}" in seg for seg in _split_outside_wildcards(path)[:-1])


def _same_or_ancestor(candidate: str, results_dir: Path) -> bool:
    """Whether `candidate` is `results_dir` or one of its ancestors, as
    spelled or by file identity."""
    targets = {os.path.abspath(candidate), os.path.realpath(candidate)}
    for start in (os.path.abspath(results_dir), os.path.realpath(results_dir)):
        current = start
        while True:
            if current in targets:
                return True
            try:
                if os.path.samefile(current, candidate):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return False


def _absolute_contains_results(path: str, results_dir: Path | None) -> bool:
    """For an absolute path with no wildcard in its directory part that may
    be a directory: whether it could be the results dir or an ancestor of it
    (a literal path naming one, or a final-component wildcard directly
    under one)."""
    if results_dir is None:
        return False
    segs = _split_outside_wildcards(path)
    if "{" in segs[-1] or "}" in segs[-1]:
        return _same_or_ancestor("/".join(segs[:-1]) or "/", results_dir)
    return _same_or_ancestor(path, results_dir)


def _wildcard_absolute_message(what: str, results_dir: Path | None) -> str:
    return (
        f"{what} is absolute, does not lie under the scanned results "
        f"directory `{results_dir}` as written, and has a wildcard in a "
        f"directory part, so snakeprune cannot tell whether some wildcard "
        f"value (or a symlink) makes it name files inside that directory. "
        f"Refusing: dropping it could list live files as orphans. Pass "
        f"{WILDCARD_ABSOLUTE_FLAG} to drop such paths with a warning instead; "
        f"do so only if they never lead into the results directory, since "
        f"any live file they name there would then be listed, and deleted "
        f"by --delete."
    )


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
    return build_rule_patterns(
        load_rule_specs(pipeline_dir, configfiles=configfiles),
        resolve_workdir(pipeline_dir),
        default_wildcard_regex=default_wildcard_regex,
        on_warning=on_warning,
        results_dir=results_dir,
        match_prefix=match_prefix,
    )


def build_rule_patterns(
    specs: Sequence[RuleSpec],
    workdir: Path,
    default_wildcard_regex: str = DEFAULT_WILDCARD_REGEX,
    on_warning: Callable[[str], None] | None = None,
    results_dir: Path | None = None,
    match_prefix: str | None = None,
    allow_wildcard_absolute: bool = False,
) -> list[tuple[str, re.Pattern]]:
    """Compile already-extracted rule specs; see `find_rule_patterns`.

    An absolute path that does not lie under `results_dir` and has a
    wildcard in a directory part raises `PatternError` unless
    `allow_wildcard_absolute`, when it is dropped with a warning; a
    `directory()` output that may be the results dir or an ancestor raises.

    `workdir` is the directory the specs' relative paths are relative to; a
    leading `..` is resolved against it. Literal path text is normalised to
    Unicode NFC, so compare against NFC-normalised file paths.
    """
    out: list[tuple[str, re.Pattern]] = []
    for spec in specs:
        bodies = rule_wildcard_bodies(spec, default_wildcard_regex)
        for output_str in spec.outputs:
            is_directory = output_str in spec.directories
            raw_path = output_str
            output_str = unicodedata.normalize("NFC", output_str)
            output_str, dotdot = normalise_rule_path(output_str)
            if dotdot and output_str.startswith("../"):
                output_str, dotdot = normalise_rule_path(
                    str(workdir) + "/" + output_str
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
                    what = f"Rule `{spec.name}` path `{raw_path}`"
                    if results_dir is not None and _wildcard_in_directory_part(output_str):
                        if not allow_wildcard_absolute:
                            raise PatternError(_wildcard_absolute_message(what, results_dir))
                        if on_warning is not None:
                            on_warning(
                                f"WARNING: dropping absolute path `{raw_path}` "
                                f"from rule `{spec.name}` ({WILDCARD_ABSOLUTE_FLAG}): "
                                f"any live file it names inside the results "
                                f"directory will be listed as an orphan."
                            )
                        continue
                    if is_directory and _absolute_contains_results(output_str, results_dir):
                        raise PatternError(
                            f"{what} is a directory() output that may be the "
                            f"scanned results directory `{results_dir}` or "
                            f"contain it, so every file there may be live. "
                            f"Refusing to scan it."
                        )
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
                overrides=bodies,
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


def rule_wildcard_bodies(spec: RuleSpec, default: str) -> dict[str, str]:
    """The regex body each wildcard of a rule must accept, in every path.

    A job's wildcard values come from matching a requested file against any
    one of the rule's products, its outputs, logs and benchmarks alike
    (Snakemake's `Rule.products`), each under its own effective constraint
    (inline, then rule-level / global, then `default`); every product and
    input is then formatted with those values. So each wildcard accepts the
    union of its constraints over all of `spec.outputs` (which holds logs and
    benchmarks too). One distinct constraint is
    used as is (the regex is unchanged); several become an alternation, with
    `default` first when an output leaves the wildcard unconstrained (which
    `AmbiguityExplainer` relies on). A constraint with numeric
    backreferences cannot be alternated safely, so the union is then
    widened to Snakemake's own `.+`.
    """
    bodies: dict[str, list[str]] = {}
    for output_str in spec.outputs:
        seen: set[str] = set()
        for m in _WILDCARD_RE.finditer(output_str):
            name = m.group("name")
            if name in seen:
                continue
            seen.add(name)
            body = m.group("constraint") or spec.constraints.get(name) or default
            found = bodies.setdefault(name, [])
            if body not in found:
                found.append(body)
    out: dict[str, str] = {}
    for name, found in bodies.items():
        if len(found) == 1:
            out[name] = found[0]
        elif any(_NUMBERED_GROUP_RE.search(b) for b in found):
            out[name] = SNAKEMAKE_WILDCARD_REGEX
        else:
            if default in found:
                found = [default] + [b for b in found if b != default]
                out[name] = default + "".join(f"|(?:{b})" for b in found[1:])
            else:
                out[name] = "|".join(f"(?:{b})" for b in found)
    return out


class InputMatcher:
    """Matches files against the paths rules read, fast enough for tens of
    thousands of input patterns.

    Workflows often expand an input over thousands of literal names
    (``expand(..., gene=GENES, allow_missing=True)``), so a single combined
    regex would be far too slow. Patterns whose final path segment is literal
    are bucketed by it: a file is tested only against patterns that share its
    name, or (since an input may be a directory) its parent directories'
    names. The few whose final segment has a wildcard form one combined regex.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, list[tuple[str, re.Pattern]]] = {}
        self._generic: list[tuple[str, re.Pattern]] = []
        self._generic_combined: CombinedPattern | None = None
        self._count = 0

    def _add(
        self,
        rule: str,
        path: str,
        constraints: dict[str, str],
        default: str,
        overrides: dict[str, str] | None = None,
    ) -> None:
        last = _split_outside_wildcards(path)[-1]
        if _WILDCARD_RE.search(last) is None:
            rx = re.compile(wildcard_pattern_to_regex(
                path, constraints, default=default, overrides=overrides,
            ))
            self._by_name.setdefault(last, []).append((rule, rx))
        else:
            rx = re.compile(wildcard_pattern_to_regex(
                path, constraints, default=default, is_directory=True,
                overrides=overrides,
            ))
            self._generic.append((rule, rx))
        self._count += 1

    def _finish(self) -> None:
        self._generic_combined = combine_rule_patterns(self._generic)

    def __len__(self) -> int:
        return self._count

    def _candidates(self, target: str):
        """Yield ``(rule, regex, text)``: each pattern worth trying, and the
        prefix of ``target`` (itself, or an ancestor directory) to try it on."""
        parts = target.split("/")
        for k in range(len(parts), 0, -1):
            bucket = self._by_name.get(parts[k - 1])
            if bucket:
                prefix = "/".join(parts[:k])
                for rule, rx in bucket:
                    yield rule, rx, prefix

    def match(self, target: str) -> bool:
        if self._generic_combined is not None and self._generic_combined.match(target):
            return True
        return any(rx.match(text) for _, rx, text in self._candidates(target))

    def find(self, target: str) -> tuple[str, re.Pattern, re.Match[str]] | None:
        """The first pattern matching ``target``, for reporting."""
        for rule, rx in self._generic:
            m = rx.match(target)
            if m:
                return rule, rx, m
        for rule, rx, text in self._candidates(target):
            m = rx.match(text)
            if m:
                return rule, rx, m
        return None


def _unusable_input(rule: str, raw: str, why: str) -> PatternError:
    return PatternError(
        f"Cannot use input `{raw}` of rule `{rule}`: {why}. It may name a "
        f"file inside the scanned results directory, so snakeprune refuses "
        f"rather than risk listing it as an orphan."
    )


def build_input_patterns(
    specs: Sequence[RuleSpec],
    workdir: Path,
    default_wildcard_regex: str = DEFAULT_WILDCARD_REGEX,
    results_dir: Path | None = None,
    match_prefix: str | None = None,
    allow_wildcard_absolute: bool = False,
    on_warning: Callable[[str], None] | None = None,
) -> InputMatcher:
    """Matcher for the paths rules read, which are live whether or not a rule
    produces them.

    Kept apart from `build_rule_patterns` on purpose: the guards that ask
    "does this workflow write here at all?" must look at outputs only.
    Input wildcards take the union of the rule's output constraints (see
    `rule_wildcard_bodies`), since their values come from matching the
    outputs. Each input also covers everything
    beneath it, in case it is a directory. An input outside the results tree
    can never be deleted, so one that is absolute and lies elsewhere, or that
    cannot be parsed but plainly lies elsewhere, is skipped without comment.
    One that may lie inside the tree but cannot be turned into a pattern
    (`..` after a wildcard, unparseable braces inside the tree, the working
    directory itself, an absolute directory that may contain the results
    dir) raises `PatternError`, as an unusable output does: skipping it
    could list a live file as an orphan. An absolute input outside the
    results dir with a wildcard in a directory part raises too, unless
    `allow_wildcard_absolute`, when it is skipped with a warning.
    """
    matcher = InputMatcher()
    for spec in specs:
        bodies = rule_wildcard_bodies(spec, default_wildcard_regex)
        for raw in spec.inputs:
            path, dotdot = normalise_rule_path(unicodedata.normalize("NFC", raw))
            if dotdot and path.startswith("../"):
                path, dotdot = normalise_rule_path(str(workdir) + "/" + path)
            if dotdot:
                raise _unusable_input(spec.name, raw, "`..` after a wildcard cannot be resolved")
            if not path:
                if _may_lie_in_results(".", results_dir, match_prefix, contains=True):
                    raise _unusable_input(
                        spec.name, raw, "it names the working directory, which holds the results"
                    )
                continue
            if path.startswith("/"):
                rewritten = _rewrite_under_results_dir(path, results_dir, match_prefix)
                if rewritten is None:
                    if results_dir is not None and _wildcard_in_directory_part(path):
                        if not allow_wildcard_absolute:
                            raise PatternError(_wildcard_absolute_message(
                                f"The input `{raw}` of rule `{spec.name}`", results_dir
                            ))
                        if on_warning is not None:
                            on_warning(
                                f"WARNING: not protecting absolute input `{raw}` "
                                f"of rule `{spec.name}` ({WILDCARD_ABSOLUTE_FLAG}): "
                                f"any file it names inside the results directory "
                                f"will be listed as an orphan."
                            )
                        continue
                    if _absolute_contains_results(path, results_dir):
                        raise _unusable_input(
                            spec.name, raw,
                            "it may be the results directory or contain it",
                        )
                    continue
                path = rewritten
            try:
                matcher._add(
                    spec.name, path, spec.constraints, default_wildcard_regex, bodies
                )
            except (PatternError, re.error) as exc:
                if _may_lie_in_results(path, results_dir, match_prefix, contains=True):
                    raise _unusable_input(
                        spec.name, raw, f"it cannot be turned into a pattern ({exc})"
                    ) from None
                continue
    matcher._finish()
    return matcher


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
