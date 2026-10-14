"""Standalone rule extractor for snakeprune.

This script is invoked as a subprocess by snakeprune's CLI:
    python _extract.py <pipeline_dir> [--configfile PATH ...]

It loads the Snakemake workflow at <pipeline_dir>, iterates over each path a
rule produces (output, log, benchmark), and emits the result as a JSON document
on stdout:

    {"rules": [{"name": str,
                "outputs": list[str],       # raw path strings, inline {name,regex} kept
                "directories": list[str],   # subset of outputs flagged directory()
                "constraints": dict[str, str]}, ...]}

Paths are emitted exactly as Snakemake stores them: Snakemake has already
inlined rule-level and global wildcard_constraints into each string as
`{name,regex}`. `constraints` (rule-local merged over global) is kept only as
a fallback for wildcards that carry no inline annotation. All brace parsing
happens in snakeprune's patterns.py — deliberately in one place.

Errors go to stderr; the script exits nonzero on any failure.

Standalone: this file must not import anything from `snakeprune`. snakeprune
the CLI ships it alongside `patterns.py` but does not import from it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_snakefile(pipeline_dir: Path) -> Path:
    candidates = [pipeline_dir / "Snakefile", pipeline_dir / "workflow" / "Snakefile"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No Snakefile found at either:\n"
        f"  {candidates[0]}\n"
        f"  {candidates[1]}"
    )


def _resolve_workdir(snakefile: Path) -> Path:
    """Working directory Snakemake would use for this Snakefile.

    Snakemake's project-layout convention: a Snakefile under a directory named
    `workflow/` is run from that directory's parent (the project root), so paths
    like `resources/...` and `results/...` resolve against the root. Anywhere
    else, the Snakefile's own directory is the working directory.
    """
    parent = snakefile.resolve().parent
    if parent.name == "workflow":
        return parent.parent
    return parent


def _load_workflow(snakefile: Path, workdir: Path, configfiles: list[Path]) -> dict:
    from snakemake.api import SnakemakeApi
    from snakemake.io import is_flagged
    from snakemake.settings.enums import Quietness
    from snakemake.settings.types import (
        ConfigSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    with SnakemakeApi(OutputSettings(quiet={Quietness.ALL})) as api:
        workflow_api = api.workflow(
            resource_settings=ResourceSettings(),
            config_settings=ConfigSettings(configfiles=configfiles),
            storage_settings=StorageSettings(),
            workflow_settings=WorkflowSettings(),
            snakefile=snakefile,
            workdir=workdir,
        )
        workflow = workflow_api._workflow
        global_constraints = dict(
            getattr(workflow, "wildcard_constraints", {}) or {}
        )
        rules_out: list[dict] = []
        for rule in workflow.rules:
            produced = list(rule.output) + list(rule.log)
            if rule.benchmark:
                produced.append(rule.benchmark)
            raw_outputs = list(dict.fromkeys(str(o) for o in produced))
            directories = [str(o) for o in rule.output if is_flagged(o, "directory")]
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "directories": directories,
                    "constraints": {**global_constraints, **rule_constraints},
                }
            )
        return {"rules": rules_out}


def extract(pipeline_dir: Path, configfiles: list[Path]) -> dict:
    snakefile = _resolve_snakefile(pipeline_dir)
    return _load_workflow(
        snakefile=snakefile,
        workdir=_resolve_workdir(snakefile),
        configfiles=configfiles,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Snakemake rule outputs as JSON.",
    )
    parser.add_argument("pipeline_dir", type=Path)
    parser.add_argument(
        "--configfile",
        type=Path,
        action="append",
        default=[],
        help="Snakemake configfile; repeatable.",
    )
    args = parser.parse_args(argv)

    try:
        result = extract(args.pipeline_dir, args.configfile)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"extractor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
