"""snakeprune CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from snakeprune.delete import delete_orphans
from snakeprune.patterns import (
    SnakefileNotFound,
    combine_rule_patterns,
    extract_literal_prefix,
    find_rule_patterns,
)
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
    allow_empty_rules: bool = typer.Option(
        False,
        "--allow-empty-rules",
        help="Bypass refusal when the workflow has 0 output patterns.",
    ),
    allow_basename_mismatch: bool = typer.Option(
        False,
        "--allow-basename-mismatch",
        help="Bypass refusal when no rule writes under the results-dir basename.",
    ),
    orphan_rate_threshold: float = typer.Option(
        0.5,
        "--orphan-rate-threshold",
        help="Fraction (0.0-1.0) above which the high-orphan-rate warning fires "
             "(default 0.5; pass 1.0 to disable).",
    ),
    allow_high_orphan_rate: bool = typer.Option(
        False,
        "--allow-high-orphan-rate",
        help="Bypass --delete refusal when orphan rate exceeds threshold.",
    ),
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
    if not patterns and not allow_empty_rules:
        typer.echo(
            "Workflow loaded but produced 0 output patterns. Refusing to "
            "scan: every file would be reported as an orphan. Check that "
            "the right config / env is loaded, or pass --allow-empty-rules "
            "to override.",
            err=True,
        )
        raise typer.Exit(code=3)
    if patterns and not allow_basename_mismatch:
        results_prefix = results_dir.name + "/"
        rule_prefixes = [extract_literal_prefix(p) for _, p in patterns]
        if not any(rp.startswith(results_prefix) for rp in rule_prefixes):
            # Surface up to 3 most common first-segments to help the user.
            from collections import Counter
            first_segs = Counter(rp.split("/", 1)[0] + "/" for rp in rule_prefixes if rp)
            top = ", ".join(f"`{seg}`" for seg, _ in first_segs.most_common(3))
            typer.echo(
                f"No rule writes under `{results_prefix}`. Rules write under: "
                f"{top}. Did you point at the wrong directory? Pass "
                f"--allow-basename-mismatch to override.",
                err=True,
            )
            raise typer.Exit(code=3)
    combined = combine_rule_patterns(patterns)

    log(f"Walking {results_dir}...")
    orphans: list[OrphanFile] = []
    file_count = 0
    target_prefix = results_dir.name + "/"
    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
    ):
        if limit is not None and file_count >= limit:
            break
        file_count += 1
        if file_count % PROGRESS_INTERVAL == 0:
            log(f"  scanned {file_count} files...")
        match_target = target_prefix + rel
        if combined is not None and combined.match(match_target):
            continue
        likely = attribute_orphan_to_rule(match_target, patterns) if rule_attribution else None
        orphans.append(OrphanFile(path=Path(full_path), rel=rel, likely_rule=likely))
    log(f"Scanned {file_count} file(s); found {len(orphans)} orphan(s).")

    high_rate = False
    if file_count > 0:
        rate = len(orphans) / file_count
        if rate > orphan_rate_threshold:
            high_rate = True
            pct = round(rate * 100)
            typer.echo(
                f"WARNING: {pct}% of scanned files are orphans "
                f"({len(orphans)} of {file_count}). This is unusually high and "
                f"usually indicates a config or environment problem rather than "
                f"real cleanup. Review the list carefully before deleting.",
                err=True,
            )

    for orphan in orphans:
        line = str(orphan.path)
        if rule_attribution and orphan.likely_rule:
            line += f"\t(likely: {orphan.likely_rule})"
        typer.echo(line)

    if delete and orphans:
        delete_orphans(orphans, allow_symlinks=allow_symlinks)
