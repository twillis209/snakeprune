"""snakeprune CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from snakeprune.patterns import SnakefileNotFound
from snakeprune.walker import find_orphans
from snakeprune.delete import delete_orphans

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
) -> None:
    """Scan a Snakemake project's results directory for orphan files."""
    try:
        orphans = find_orphans(
            pipeline_dir=pipeline_dir,
            results_dir=results_dir,
            ignore_globs=tuple(ignore or ()),
            follow_symlinks=follow_symlinks,
            attribute_rules=rule_attribution,
        )
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    for orphan in orphans:
        line = str(orphan.path)
        if rule_attribution and orphan.likely_rule:
            line += f"\t(likely: {orphan.likely_rule})"
        typer.echo(line)

    if delete and orphans:
        delete_orphans(orphans, allow_symlinks=allow_symlinks)
