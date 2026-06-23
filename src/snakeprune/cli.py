"""snakeprune CLI."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer

from snakeprune.delete import delete_orphans
from snakeprune.patterns import (
    ExtractorError,
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
DEFAULT_NAUGHTY_DIRS = frozenset(
    {"resources", "config", "profile", "workflow", ".snakemake"}
)


def _stdin_isatty() -> bool:
    """Return whether stdin is a TTY.  Thin wrapper so tests can monkeypatch it."""
    return sys.stdin.isatty()


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
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive Y/N prompt before deleting. Required for non-TTY use.",
    ),
    trash: Optional[Path] = typer.Option(
        None,
        "--trash",
        help="Move orphans to this directory instead of unlinking; implies delete mode.",
    ),
    configfile: Optional[list[Path]] = typer.Option(
        None,
        "--configfile",
        help="Snakemake configfile to pass to the extractor; repeatable.",
    ),
    naughty_dir: Optional[list[str]] = typer.Option(
        None,
        "--naughty-dir",
        help="Extra directory basename to treat as a suspicious target; repeatable.",
    ),
    allow_naughty_dir: bool = typer.Option(
        False,
        "--allow-naughty-dir",
        help="Bypass the delete/trash refusal when the target dir basename is on the naughty list.",
    ),
    exclude_dir: Optional[list[str]] = typer.Option(
        None,
        "--exclude-dir",
        help="Directory subtree to skip entirely; repeatable. Relative paths "
             "resolve against results_dir.",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", help="Stop after scanning N files (for benchmarking)."),
) -> None:
    """Scan a Snakemake project's results directory for orphan files."""

    def log(msg: str) -> None:
        if not quiet:
            typer.echo(msg, err=True)

    if limit is not None and (delete or trash is not None):
        typer.echo(
            "Refusing to delete: --limit truncates the scan, so the orphan set "
            "would be partial and the orphan-rate guard meaningless. Drop "
            "--limit to delete, or drop --delete/--trash to benchmark.",
            err=True,
        )
        raise typer.Exit(code=3)

    naughty = DEFAULT_NAUGHTY_DIRS | set(naughty_dir or ())
    if results_dir.name in naughty:
        typer.echo(
            f"WARNING: `{results_dir.name}` is a conventional Snakemake "
            f"input/config directory and is unlikely to contain prunable rule "
            f"outputs. Review the orphan list carefully — this may be the wrong "
            f"target.",
            err=True,
        )
        if (delete or trash is not None) and not allow_naughty_dir:
            typer.echo(
                f"Refusing to delete: `{results_dir.name}` looks like a "
                f"Snakemake input/config directory. Pass --allow-naughty-dir "
                f"to override.",
                err=True,
            )
            raise typer.Exit(code=3)

    log(f"Loading Snakemake workflow from {pipeline_dir}...")
    try:
        patterns = find_rule_patterns(
            pipeline_dir, configfiles=tuple(configfile or ())
        )
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    except ExtractorError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)
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
    walk_stats: dict = {}
    exclude_set: set[str] = set()
    for d in exclude_dir or ():
        p = Path(d)
        resolved = p if p.is_absolute() else results_dir / p
        exclude_set.add(os.path.abspath(resolved))
    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
        exclude_dirs=tuple(exclude_set),
        stats=walk_stats,
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

    skipped_dirs = walk_stats.get("skipped_symlinked_dirs", 0)
    if skipped_dirs > 0:
        suffix = "y" if skipped_dirs == 1 else "ies"
        typer.echo(
            f"Skipped {skipped_dirs} symlinked subdirector{suffix}; files "
            f"reachable only via those paths were not scanned.",
            err=True,
        )

    excluded_dirs = walk_stats.get("excluded_dirs", 0)
    if excluded_dirs > 0:
        typer.echo(
            f"Excluded {excluded_dirs} directory subtree(s) from the scan.",
            err=True,
        )

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

    deletion_requested = delete or trash is not None
    if deletion_requested and orphans:
        total_bytes = 0
        for o in orphans:
            try:
                total_bytes += o.path.stat().st_size
            except OSError:
                pass
        typer.echo(
            f"About to delete {len(orphans)} file(s), {total_bytes} byte(s).",
            err=True,
        )
        if high_rate and not allow_high_orphan_rate:
            typer.echo(
                "Refusing to delete: orphan rate exceeded threshold. Pass "
                "--allow-high-orphan-rate to override.",
                err=True,
            )
            raise typer.Exit(code=3)
        if not yes:
            if not _stdin_isatty():
                typer.echo(
                    "Refusing to delete: stdin is not a TTY. Pass --yes to "
                    "confirm in scripts.",
                    err=True,
                )
                raise typer.Exit(code=3)
            answer = typer.prompt("Proceed? [y/N]", default="n", show_default=False)
            if answer.strip().lower() not in {"y", "yes"}:
                typer.echo("Aborted.", err=True)
                raise typer.Exit(code=0)
        try:
            delete_orphans(
                orphans,
                allow_symlinks=allow_symlinks,
                trash_dir=trash,
                results_dir_name=results_dir.name if trash is not None else None,
            )
        except (PermissionError, IsADirectoryError, FileExistsError) as exc:
            typer.echo(f"Refusing to delete: {exc}", err=True)
            raise typer.Exit(code=3)
