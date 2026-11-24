"""Standalone rule extractor for snakeprune.

This script is invoked as a subprocess by snakeprune's CLI:
    python _extract.py <pipeline_dir> [--configfile PATH ...] [--directory DIR]

It loads the Snakemake workflow at <pipeline_dir>, iterates over each path a
rule produces (output, log, benchmark), and emits the result as a JSON document
on stdout:

    {"workdir": str,                        # directory rule paths are relative to
     "workdir_source": str,   # "default" | "directive" | "option" | "chdir"
     "rules": [{"name": str,
                "outputs": list[str],       # raw path strings, inline {name,regex} kept
                "directories": list[str],   # subset of outputs flagged directory()
                "inputs": list[str],        # static input paths (functions skipped)
                "dynamic_inputs": int,      # input entries that are functions
                "constraints": dict[str, str]}, ...]}

The Snakefile is parsed as a `snakemake` run from the pipeline directory
would parse it: from `--directory` (as with `snakemake -d`, which disables
any `workdir:` directive), else from the default described in
`_resolve_workdir`, with each `workdir:` directive changing directory as it
runs, so parse-time code such as `os.path.abspath` sees the directory
Snakemake would. The reported workdir is the directory the process is in
once parsing is done, which is where Snakemake runs the jobs. A directive
naming a directory that does not exist is refused: Snakemake would create
it, and the extractor never creates anything.

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
import os
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


def _relativise(path: str, workdir: Path) -> str:
    """Strip the workdir prefix from an absolute path that lies under it.

    Both the plain and the symlink-resolved form of the workdir are tried,
    because a Snakefile that calls `os.path.abspath` yields whichever form the
    subprocess's cwd happens to have. Absolute paths elsewhere are returned
    unchanged; `patterns.py` drops them with a warning.
    """
    import os

    if not os.path.isabs(path):
        return path
    for base in {os.path.abspath(workdir), str(Path(workdir).resolve())}:
        prefix = base.rstrip(os.sep) + os.sep
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


class WorkdirError(RuntimeError):
    """The directory Snakemake would run in cannot be established."""


def _load_workflow(
    snakefile: Path, workdir: Path, configfiles: list[Path], source: str = "default"
) -> dict:
    from snakemake.api import SnakemakeApi
    from snakemake.workflow import Workflow

    # Record each `workdir:` directive Snakemake honours (none under
    # --directory) and refuse one whose target is missing rather than let
    # Snakemake create it.
    directives: list[str] = []
    original_workdir = Workflow.workdir

    def _record_workdir(self, value):
        if self.overwrite_workdir is None:
            target = os.path.abspath(str(value))
            if not os.path.isdir(target):
                raise WorkdirError(
                    f"the Snakefile's `workdir: {value}` names {target}, which "
                    f"does not exist. Snakemake would create it; snakeprune "
                    f"creates nothing. Create it first (it then holds no "
                    f"results), or check the configuration."
                )
            directives.append(target)
        return original_workdir(self, value)

    Workflow.workdir = _record_workdir
    from snakemake.io import is_callable, is_flagged
    from snakemake.settings.enums import Quietness
    from snakemake.settings.types import (
        ConfigSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    snakefile = Path(os.path.abspath(snakefile))
    configfiles = [Path(os.path.abspath(c)) for c in configfiles]
    workdir = Path(os.path.abspath(workdir))
    if source == "option":
        # As `snakemake -d`: Snakemake changes into it and ignores directives.
        api_workdir: Path | None = workdir
    else:
        # As a plain `snakemake`: run from the default directory, and let
        # directives change directory while the Snakefile is parsed.
        os.chdir(workdir)
        api_workdir = None

    with SnakemakeApi(OutputSettings(quiet={Quietness.ALL})) as api:
        workflow_api = api.workflow(
            resource_settings=ResourceSettings(),
            config_settings=ConfigSettings(configfiles=configfiles),
            storage_settings=StorageSettings(),
            workflow_settings=WorkflowSettings(),
            snakefile=snakefile,
            workdir=api_workdir,
        )
        workflow = workflow_api._workflow  # parses the Snakefile
        # Jobs run wherever parsing left the process; keep the caller's
        # spelling of that directory where it names the same place.
        cwd = os.getcwd()
        if directives and os.path.samefile(directives[-1], cwd):
            workdir, source = Path(directives[-1]), "directive"
        elif not os.path.samefile(workdir, cwd):
            workdir, source = Path(cwd), "chdir"
        global_constraints = dict(
            getattr(workflow, "wildcard_constraints", {}) or {}
        )
        rules_out: list[dict] = []
        for rule in workflow.rules:
            produced = list(rule.output) + list(rule.log)
            if rule.benchmark:
                produced.append(rule.benchmark)
            for o in produced:
                if callable(o) or is_callable(o):
                    raise ValueError(
                        f"rule {rule.name}: an output/log/benchmark path is a "
                        f"function; snakeprune cannot derive a pattern from it"
                    )
            raw_outputs = list(dict.fromkeys(_relativise(str(o), workdir) for o in produced))
            directories = [
                _relativise(str(o), workdir)
                for o in rule.output
                if is_flagged(o, "directory")
            ]
            inputs: list[str] = []
            dynamic_inputs = 0
            for i in rule.input:
                if callable(i) or is_callable(i):
                    dynamic_inputs += 1
                else:
                    inputs.append(_relativise(str(i), workdir))
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "directories": directories,
                    "inputs": list(dict.fromkeys(inputs)),
                    "dynamic_inputs": dynamic_inputs,
                    "constraints": {**global_constraints, **rule_constraints},
                }
            )
        return {
            "workdir": os.path.abspath(workdir),
            "workdir_source": source,
            "rules": rules_out,
        }


def extract(
    pipeline_dir: Path, configfiles: list[Path], directory: Path | None = None
) -> dict:
    snakefile = _resolve_snakefile(pipeline_dir)
    if directory is not None:
        if not directory.is_dir():
            raise FileNotFoundError(f"--directory {directory} is not a directory")
        return _load_workflow(snakefile, directory, configfiles, "option")
    return _load_workflow(
        snakefile=snakefile,
        workdir=_resolve_workdir(snakefile),
        configfiles=configfiles,
    )


def main(argv: list[str] | None = None) -> int:
    # Keep anything the workflow prints (Python or child processes) off the
    # JSON channel: point fd 1 at stderr for the rest of the process and keep
    # the original stdout aside for the payload. Deliberately not restored.
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
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
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Working directory the workflow runs in (as `snakemake -d`).",
    )
    args = parser.parse_args(argv)

    try:
        result = extract(args.pipeline_dir, args.configfile, args.directory)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"extractor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    with os.fdopen(saved, "w") as out:
        json.dump(result, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
