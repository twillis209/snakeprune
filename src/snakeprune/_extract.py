"""Standalone rule extractor for snakeprune.

This script is invoked as a subprocess by snakeprune's CLI:
    python _extract.py <pipeline_dir> [--configfile PATH ...]

It loads the Snakemake workflow at <pipeline_dir>, iterates over each rule's
declared outputs, strips Snakemake's inline `{name,regex}` constraint
annotations, merges effective wildcard constraints, and emits the result
as a JSON document on stdout:

    {"rules": [{"name": str, "outputs": list[str], "constraints": dict[str, str]}, ...]}

Errors go to stderr; the script exits nonzero on any failure.

Standalone: this file must not import anything from `snakeprune`. snakeprune
the CLI ships it alongside `patterns.py` but does not import from it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_INLINE_CONSTRAINT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*),[^{}]*\}")


def _strip_inline_constraints(pattern: str) -> tuple[str, dict[str, str]]:
    constraints: dict[str, str] = {}

    def _record(match: re.Match[str]) -> str:
        name = match.group(1)
        body = match.group(0)[len(name) + 2 : -1]
        constraints[name] = body
        return "{" + name + "}"

    stripped = _INLINE_CONSTRAINT_RE.sub(_record, pattern)
    return stripped, constraints


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


def _load_workflow(snakefile: Path, workdir: Path, configfiles: list[Path]) -> dict:
    from snakemake.api import SnakemakeApi
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
            raw_outputs: list[str] = []
            inline_constraints: dict[str, str] = {}
            for o in rule.output:
                stripped, inline = _strip_inline_constraints(str(o))
                raw_outputs.append(stripped)
                inline_constraints.update(inline)
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            effective = {
                **global_constraints,
                **inline_constraints,
                **rule_constraints,
            }
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "constraints": effective,
                }
            )
        return {"rules": rules_out}


def extract(pipeline_dir: Path, configfiles: list[Path]) -> dict:
    snakefile = _resolve_snakefile(pipeline_dir)
    return _load_workflow(
        snakefile=snakefile,
        workdir=pipeline_dir.resolve(),
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
