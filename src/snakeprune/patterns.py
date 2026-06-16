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


# Matches Snakemake's inline wildcard-with-constraint syntax inside a pattern:
# ``{name,regex}`` -> stripped to ``{name}``. The regex body itself may contain
# ``{`` / ``}`` only when balanced (Snakemake's own parser handles that); the
# constraints we care about for our RuleSpec are surfaced via the workflow /
# rule wildcard_constraints attributes, so for stripping purposes we only need
# to remove up to the next unescaped ``}``.
_INLINE_CONSTRAINT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*),[^{}]*\}")


def _strip_inline_constraints(pattern: str) -> tuple[str, dict[str, str]]:
    """Strip ``{name,regex}`` -> ``{name}`` from a Snakemake output pattern.

    Returns a tuple of ``(stripped_pattern, inline_constraints)`` where
    ``inline_constraints`` maps each inline-annotated wildcard name to its
    regex body. Snakemake stores the raw pattern with the inline regex in
    ``rule.output`` but does NOT populate it into ``rule.wildcard_constraints``,
    so callers need this dict to recover the constraint information that would
    otherwise be discarded.
    """
    constraints: dict[str, str] = {}

    def _record(match: re.Match[str]) -> str:
        name = match.group(1)
        # Match group 0 is "{name,body}"; drop "{name," and trailing "}".
        body = match.group(0)[len(name) + 2 : -1]
        constraints[name] = body
        return "{" + name + "}"

    stripped = _INLINE_CONSTRAINT_RE.sub(_record, pattern)
    return stripped, constraints


def load_rule_specs(pipeline_dir: Path) -> list[RuleSpec]:
    """Load the Snakemake workflow at ``pipeline_dir`` and extract per-rule output specs.

    Each rule contributes a :class:`RuleSpec` with its name, the raw output
    pattern strings (with any inline ``{name,regex}`` constraint annotations
    stripped), and the effective wildcard constraints. Precedence, low-to-high:
    workflow-global < inline ``{name,regex}`` annotations on this rule's
    outputs < rule-local ``wildcard_constraints`` block.
    """
    snakefile = resolve_snakefile(pipeline_dir)

    # Local imports so the package can be imported without snakemake installed.
    from snakemake.api import SnakemakeApi
    from snakemake.settings.enums import Quietness
    from snakemake.settings.types import (
        ConfigSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    # workdir is set to pipeline_dir so relative paths in the Snakefile
    # (configfile:, include:, etc.) resolve as they would for a normal Snakemake
    # invocation from that directory. A .snakemake/ subdirectory may be created
    # there as a side effect — same place Snakemake itself would put it.
    with SnakemakeApi(OutputSettings(quiet={Quietness.ALL})) as api:
        workflow_api = api.workflow(
            resource_settings=ResourceSettings(),
            config_settings=ConfigSettings(),
            storage_settings=StorageSettings(),
            workflow_settings=WorkflowSettings(),
            snakefile=snakefile,
            workdir=pipeline_dir.resolve(),
        )
        # Snakemake 9's WorkflowApi parses the Snakefile lazily; accessing the
        # underlying ``_workflow`` after constructing the WorkflowApi gives us
        # the populated ``Workflow`` object with rules and wildcard_constraints
        # available. No need to build the DAG.
        workflow = workflow_api._workflow
        global_constraints = dict(getattr(workflow, "wildcard_constraints", {}) or {})

        specs: list[RuleSpec] = []
        for rule in workflow.rules:
            raw_outputs: list[str] = []
            inline_constraints: dict[str, str] = {}
            for o in rule.output:
                stripped, inline = _strip_inline_constraints(str(o))
                raw_outputs.append(stripped)
                # Last inline annotation wins if a wildcard is annotated more
                # than once across this rule's outputs; rule-local declarations
                # override below regardless.
                inline_constraints.update(inline)
            rule_constraints = dict(getattr(rule, "wildcard_constraints", {}) or {})
            # Merge precedence (low -> high):
            #   workflow-global  <  inline {name,regex}  <  rule-local
            effective = {
                **global_constraints,
                **inline_constraints,
                **rule_constraints,
            }
            specs.append(
                RuleSpec(name=rule.name, outputs=raw_outputs, constraints=effective)
            )

    return specs


def find_rule_patterns(pipeline_dir: Path) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per output pattern.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern.
    """
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(output_str, spec.constraints)
            out.append((spec.name, re.compile(regex_str)))
    return out
