"""snakeprune CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from snakeprune.delete import delete_orphans
from snakeprune.patterns import SnakefileNotFound, find_rule_patterns
from snakeprune.walker import (
    OrphanFile,
    attribute_orphan_to_rule,
    iter_results_files,
)

PROGRESS_INTERVAL = 10000

app = typer.Typer(add_completion=False, help="Find orphan files in a Snakemake results tree.")


@app.callback()
def _main() -> None:
    """snakeprune: find orphan files in a Snakemake results tree."""


@app.command()
def scan(
    pipeline_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    results_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    delete: bool = typer.Option(False, "--delete", help="Unlink orphans (default: dry-run only)."),
    rule_attribution: bool = typer.Option(False, "--rule-attribution", help="Show best-guess rule per orphan."),
    ignore: Optional[list[str]] = typer.Option(None, "--ignore", help="Glob pattern to skip; repeatable."),
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks", help="Follow symlinks (default: skip)."),
    allow_symlinks: bool = typer.Option(False, "--allow-symlinks", help="Allow deleting symlinks when --delete is set."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress messages on stderr."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Stop after scanning N files (for benchmarking)."),
) -> None:
    """Scan a Snakemake project's results directory for orphan files."""

    def log(msg: str) -> None:
        if not quiet:
            typer.echo(msg, err=True)

    log(f"Loading Snakemake workflow from {pipeline_dir}...")
    try:
        patterns = find_rule_patterns(pipeline_dir)
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    log(f"Loaded {len(patterns)} rule output pattern(s).")

    log(f"Walking {results_dir}...")
    orphans: list[OrphanFile] = []
    file_count = 0
    for path in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
    ):
        if limit is not None and file_count >= limit:
            break
        file_count += 1
        if file_count % PROGRESS_INTERVAL == 0:
            log(f"  scanned {file_count} files...")
        match_target = results_dir.name + "/" + path.relative_to(results_dir).as_posix()
        if any(p.match(match_target) for _, p in patterns):
            continue
        likely = attribute_orphan_to_rule(match_target, patterns) if rule_attribution else None
        orphans.append(OrphanFile(path=path, likely_rule=likely))
    log(f"Scanned {file_count} file(s); found {len(orphans)} orphan(s).")

    for orphan in orphans:
        line = str(orphan.path)
        if rule_attribution and orphan.likely_rule:
            line += f"\t(likely: {orphan.likely_rule})"
        typer.echo(line)

    if delete and orphans:
        delete_orphans(orphans, allow_symlinks=allow_symlinks)
