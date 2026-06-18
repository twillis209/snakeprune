"""Build regexes from Snakemake rule output patterns."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

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


from dataclasses import dataclass


@dataclass(frozen=True)
class RuleSpec:
    """Raw rule output information extracted from a loaded workflow.

    Fields:
        name: the rule name as declared in the Snakefile.
        outputs: raw output pattern strings (e.g. ``"results/{x}.txt"``) with any
            inline ``{name,regex}`` constraint annotations stripped — the regex
            constraint, if any, is reported separately via ``constraints``.
        constraints: effective wildcard constraints (rule-local merged over
            workflow-global). Keys are wildcard names, values are regex bodies.
    """
    name: str
    outputs: list[str]
    constraints: dict[str, str]


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
        raise ExtractorError(
            f"Extractor failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractorError(
            "Extractor produced unparseable output. This is a bug; please "
            f"report. stderr was:\n{result.stderr.strip()}"
        ) from exc

    return [
        RuleSpec(
            name=r["name"],
            outputs=list(r["outputs"]),
            constraints=dict(r["constraints"]),
        )
        for r in payload["rules"]
    ]


def find_rule_patterns(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per output pattern.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern.
    """
    # Pre-check: surface SnakefileNotFound before spawning the extractor
    # subprocess, so a missing Snakefile exits 2 (not 4 via ExtractorError).
    resolve_snakefile(pipeline_dir)
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(output_str, spec.constraints)
            out.append((spec.name, re.compile(regex_str)))
    return out


def extract_literal_prefix(compiled: re.Pattern) -> str:
    """Return the un-escaped literal prefix of an anchored rule regex.

    Reads ``compiled.pattern`` up to the first ``(`` (the first capture group)
    and reverses ``re.escape``-style backslash escaping so the result is a
    real path prefix. Returns the entire literal body (minus the trailing
    ``$``) if the pattern has no capture group.
    """
    body = compiled.pattern.lstrip("^")
    if body.endswith("$"):
        body = body[:-1]
    cut = body.find("(")
    literal = body if cut == -1 else body[:cut]
    return re.sub(r"\\(.)", r"\1", literal)


_NAMED_GROUP_OPEN_RE = re.compile(r"\(\?P<([A-Za-z_][A-Za-z_0-9]*)>")
_NAMED_BACKREF_RE = re.compile(r"\(\?P=([A-Za-z_][A-Za-z_0-9]*)\)")


def combine_rule_patterns(
    patterns: list[tuple[str, re.Pattern]],
) -> re.Pattern | None:
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

    The match semantics are unchanged from running each input regex separately.
    """
    if not patterns:
        return None
    parts: list[str] = []
    for i, (_, compiled) in enumerate(patterns):
        body = compiled.pattern
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
    return re.compile("^(?:" + "|".join(parts) + ")$")
