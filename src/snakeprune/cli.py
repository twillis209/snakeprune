"""snakeprune CLI."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

import typer

from snakeprune.delete import DeletionFailed, delete_orphans
from snakeprune.patterns import (
    DEFAULT_WILDCARD_REGEX,
    SNAKEMAKE_WILDCARD_REGEX,
    ExtractorError,
    PatternError,
    SnakefileNotFound,
    combine_rule_patterns,
    find_rule_patterns,
    results_match_prefix,
    rule_literal_prefixes,
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


class ExcludeDirError(ValueError):
    """An --exclude-dir value that cannot be honoured; carries the reason."""

    def __init__(self, value: str, resolved: str, reason: str) -> None:
        super().__init__(
            f"--exclude-dir `{value}` (resolved to `{resolved}`) {reason}. "
            "Relative paths resolve against the results directory."
        )


def _nested_rel(path: str, base: str) -> Optional[str]:
    """Return ``path`` relative to ``base`` if strictly inside it, else None."""
    if path.startswith(base.rstrip(os.sep) + os.sep):
        return path[len(base.rstrip(os.sep)) + 1:]
    return None


def _resolve_exclude_dirs(results_dir: Path, values: Sequence[str]) -> list[str]:
    """Validate ``--exclude-dir`` values and return them in the form the
    walker compares against: ``abspath(join(results_dir, rel))``.

    Each value must name an existing, non-symlink directory strictly inside
    ``results_dir`` (lexically or after symlink resolution).
    """
    out: list[str] = []
    base = os.fspath(results_dir)
    for value in values:
        joined = value if os.path.isabs(value) else os.path.join(base, value)
        lexical = os.path.abspath(joined)
        if not os.path.lexists(lexical):
            raise ExcludeDirError(value, lexical, "does not exist")
        if os.path.islink(lexical):
            raise ExcludeDirError(value, lexical, "is a symlink; the scan never descends into symlinked directories")
        if not os.path.isdir(lexical):
            raise ExcludeDirError(value, lexical, "is not a directory")
        rel = _nested_rel(lexical, os.path.abspath(base))
        if rel is None:
            rel = _nested_rel(os.path.realpath(lexical), os.path.realpath(base))
        if rel is None:
            raise ExcludeDirError(value, lexical, "is not strictly inside the results directory")
        target = os.path.abspath(os.path.join(base, rel))
        if os.path.islink(target) or not os.path.isdir(target):
            raise ExcludeDirError(value, target, "is not a real directory inside the results directory")
        out.append(target)
    return out


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
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks", help="Include symlinked files (symlinked directories are never descended into)."),
    allow_symlinks: bool = typer.Option(False, "--allow-symlinks", help="Allow deleting symlinks when --delete is set."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress messages on stderr."),
    allow_empty_rules: bool = typer.Option(
        False,
        "--allow-empty-rules",
        help="Bypass refusal when the workflow has 0 path patterns.",
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
        help="Directory subtree to skip entirely; repeatable. Must be an "
             "existing, non-symlink directory strictly inside results_dir, "
             "else the scan is refused (exit 3). Relative paths resolve "
             "against results_dir, not the current directory.",
    ),
    permissive_wildcards: bool = typer.Option(
        False,
        "--permissive-wildcards",
        help="Use Snakemake's own `.+` for unconstrained wildcards (spans `/`). "
             "Default is the stricter `[^/]+`, which is what lets snakeprune "
             "detect files from older, differently-nested output layouts.",
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

    if trash is not None:
        # Compare both lexical and symlink-resolved forms so a symlinked
        # results dir cannot be bypassed via its real path (or vice versa).
        inside = any(
            t == r or t.startswith(r + os.sep)
            for t, r in (
                (os.path.abspath(trash), os.path.abspath(results_dir)),
                (os.path.realpath(trash), os.path.realpath(results_dir)),
            )
        )
        if inside:
            typer.echo(
                "Refusing to delete: --trash must not be inside the results "
                "directory being scanned. The next scan would report the "
                "trashed files as orphans and --delete would unlink the only "
                "copy. Choose a trash directory outside the results tree.",
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

    try:
        exclude_dirs = _resolve_exclude_dirs(results_dir, exclude_dir or ())
    except ExcludeDirError as exc:
        typer.echo(f"Refusing to scan: {exc}", err=True)
        raise typer.Exit(code=3)

    log(f"Loading Snakemake workflow from {pipeline_dir}...")
    try:
        match_prefix = results_match_prefix(pipeline_dir, results_dir)
        if match_prefix == "" and (delete or trash is not None) and not allow_basename_mismatch:
            typer.echo(
                "Refusing to delete: the scanned directory is the workflow's "
                "working directory, so the Snakefile, config and inputs would "
                "be listed as orphans. Scan the results directory instead, or "
                "pass --allow-basename-mismatch to override.",
                err=True,
            )
            raise typer.Exit(code=3)
        patterns = find_rule_patterns(
            pipeline_dir,
            configfiles=tuple(configfile or ()),
            default_wildcard_regex=(
                SNAKEMAKE_WILDCARD_REGEX if permissive_wildcards else DEFAULT_WILDCARD_REGEX
            ),
            on_warning=lambda msg: typer.echo(msg, err=True),
            results_dir=results_dir,
            match_prefix=match_prefix,
        )
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    except (ExtractorError, PatternError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)
    log(f"Loaded {len(patterns)} rule path pattern(s) (output, log, benchmark).")
    if not patterns and not allow_empty_rules:
        typer.echo(
            "Workflow loaded but produced 0 path patterns. Refusing to "
            "scan: every file would be reported as an orphan. Check that "
            "the right config / env is loaded, or pass --allow-empty-rules "
            "to override.",
            err=True,
        )
        raise typer.Exit(code=3)
    prefixes = rule_literal_prefixes(patterns)
    # The workflow may have run in a different directory than the extractor
    # assumed (`workdir:` / `-d`), so a file is also live if it matches under
    # the results-dir basename.
    basename_prefix = results_dir.name + "/"
    if patterns and not allow_basename_mismatch:
        results_prefix = match_prefix
        rule_prefixes = [prefix for _, prefix in prefixes]
        if not any(
            rp.startswith(results_prefix) or rp.startswith(basename_prefix)
            for rp in rule_prefixes
        ):
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
    try:
        combined = combine_rule_patterns(patterns)
    except PatternError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)

    log(f"Walking {results_dir}...")
    orphans: list[OrphanFile] = []
    file_count = 0
    target_prefixes = [match_prefix]
    if basename_prefix != match_prefix:
        target_prefixes.append(basename_prefix)
    walk_stats: dict = {}
    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
        exclude_dirs=tuple(set(exclude_dirs)),
        stats=walk_stats,
    ):
        if limit is not None and file_count >= limit:
            break
        file_count += 1
        if file_count % PROGRESS_INTERVAL == 0:
            log(f"  scanned {file_count} files...")
        targets = [tp + rel for tp in target_prefixes]
        if combined is not None and any(combined.match(t) for t in targets):
            continue
        likely = None
        if rule_attribution:
            for t in targets:
                likely = attribute_orphan_to_rule(t, patterns, prefixes)
                if likely:
                    break
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
        except DeletionFailed as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=5)
        except (PermissionError, IsADirectoryError, FileExistsError) as exc:
            typer.echo(f"Refusing to delete: {exc}", err=True)
            raise typer.Exit(code=3)
