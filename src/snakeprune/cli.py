"""snakeprune CLI."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

import typer

from snakeprune.delete import DeletionFailed, delete_orphans, validate_orphans
from snakeprune.patterns import (
    DEFAULT_WILDCARD_REGEX,
    PrefixResolver,
    SNAKEMAKE_WILDCARD_REGEX,
    ExtractorError,
    PatternError,
    SnakefileNotFound,
    build_input_patterns,
    build_rule_patterns,
    combine_rule_patterns,
    extract_workflow,
    resolve_snakefile,
    results_dir_name,
    results_match_prefix,
    rule_literal_prefixes,
)
from snakeprune.ignorefile import (
    IGNORE_FILE_NAME,
    IgnoreFileError,
    find_ancestor_ignore_files,
    load_ignore_file,
)
from snakeprune.report import (
    AmbiguityExplainer,
    DirectorySummary,
    OrphanSelection,
    OrphanSpool,
    human_size,
)
from snakeprune.walker import (
    _compile_globs,
    attribute_orphan_to_rule,
    iter_results_files,
    nfc,
)

PROGRESS_INTERVAL = 10000
DEFAULT_NAUGHTY_DIRS = frozenset(
    {"resources", "config", "profile", "workflow", ".snakemake"}
)


class ExcludeDirError(ValueError):
    """An --exclude-dir value that cannot be honoured; carries the reason."""

    def __init__(self, value: str, resolved: str, reason: str, label: str = "--exclude-dir") -> None:
        self.value, self.resolved, self.reason = value, resolved, reason
        super().__init__(
            f"{label} `{value}` (resolved to `{resolved}`) {reason}. "
            "Relative paths resolve against the results directory."
        )


def _nested_rel(path: str, base: str) -> Optional[str]:
    """Return ``path`` relative to ``base`` if strictly inside it, else None."""
    if path.startswith(base.rstrip(os.sep) + os.sep):
        return path[len(base.rstrip(os.sep)) + 1:]
    return None


def _resolve_exclude_dirs(
    results_dir: Path, values: Sequence[str], labels: Sequence[str] | None = None
) -> list[str]:
    """Validate ``--exclude-dir`` values and return them in the form the
    walker compares against: ``abspath(join(results_dir, rel))``.

    Each value must name an existing, non-symlink directory strictly inside
    ``results_dir`` (lexically or after symlink resolution), spelled as on
    disk, with no symlinked directory between ``results_dir`` and it.
    """
    out: list[str] = []
    base = os.fspath(results_dir)
    for index, value in enumerate(values):
        label = labels[index] if labels is not None else "--exclude-dir"
        try:
            out.append(_resolve_exclude_dir(base, value))
        except ExcludeDirError as exc:
            if label == "--exclude-dir":
                raise
            raise ExcludeDirError(exc.value, exc.resolved, exc.reason, label=label) from None
    return out


def _resolve_exclude_dir(base: str, value: str) -> str:
    """Validate one directory (see ``_resolve_exclude_dirs``)."""
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
    # The walker compares path strings exactly, so every component must be
    # spelled as on disk (case-insensitive filesystems accept other cases).
    parent = base
    for part in rel.split(os.sep):
        if part not in os.listdir(parent):
            raise ExcludeDirError(
                value, target,
                "does not match the on-disk spelling of any directory (check letter case)",
            )
        parent = os.path.join(parent, part)
        # The walk never passes through a symlinked directory, so it
        # reaches these files by another path that this would not exclude.
        if os.path.islink(parent):
            raise ExcludeDirError(
                value, target,
                f"is reached through the symlink `{parent}`; name the "
                "directory by its real path",
            )
    return target


def _is_inside(path: Path, directory: Path) -> bool:
    """Whether ``path`` (which need not exist) is ``directory`` or lies in it.

    Compares by file identity as well as by spelling, so a symlink or (on a
    case-insensitive filesystem) a different letter case cannot hide it.
    """
    for form, dir_form in (
        (os.path.abspath(path), os.path.abspath(directory)),
        (os.path.realpath(path), os.path.realpath(directory)),
    ):
        if form == dir_form or form.startswith(dir_form.rstrip(os.sep) + os.sep):
            return True
        current = form
        while True:
            try:
                if os.path.samefile(current, directory):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return False


def _find_results_aliases(
    resolver: PrefixResolver, match_prefixes: Sequence[str]
) -> list[tuple[str, str]]:
    """Symlinks a wildcard can pass through into the results tree, as
    ``(link, resolved target)`` pairs.

    Only the trees under ``resolver.scan_roots`` are searched: the resolved
    literal prefixes of relative wildcard paths that start outside the
    results tree (a prefix inside it is covered by the walk). Nested roots
    are searched once; symlinks are never followed; the results tree is
    skipped by identity; broken links are ignored. A link counts if it
    resolves inside, onto or above the results tree, by identity. A link
    whose workdir-relative path is a leading part of a match prefix is how
    scanned files are spelled for matching, so it is not an alias.
    """
    roots: list[str] = []
    for root in sorted(resolver.scan_roots):
        if not any(root == k or root.startswith(k.rstrip(os.sep) + os.sep) for k in roots):
            roots.append(root)
    workdir = resolver.real_workdir.rstrip(os.sep) + os.sep
    hits: list[tuple[str, str]] = []
    for root in roots:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                it = os.scandir(current)
            except OSError:
                continue
            with it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            target = os.path.realpath(entry.path)
                            if not os.path.exists(target):
                                continue
                            if resolver.where(target)[0] == "outside":
                                continue
                            if entry.path.startswith(workdir):
                                rel = entry.path[len(workdir):].replace(os.sep, "/") + "/"
                                if any(p.startswith(rel) for p in match_prefixes):
                                    continue
                            hits.append((entry.path, target))
                        elif entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if (st.st_dev, st.st_ino) != resolver.results_id:
                                stack.append(entry.path)
                    except OSError:
                        continue
    return sorted(hits)


def _same_file(a, b) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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
        help="Bypass the refusals when no rule writes under the results-dir "
             "basename, or the results dir is the workflow working directory.",
    ),
    orphan_rate_threshold: float = typer.Option(
        0.5,
        "--orphan-rate-threshold",
        help="Fraction (0.0-1.0) of scanned files (excluding those kept only "
             "as inputs) above which the scan is refused (default 0.5; pass "
             "1.0 to disable).",
    ),
    allow_high_orphan_rate: bool = typer.Option(
        False,
        "--allow-high-orphan-rate",
        help="Bypass the refusal (dry run or delete) when the orphan rate "
             "exceeds the threshold.",
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
        help="Bypass the refusal (dry run or delete) when the target dir "
             "basename is on the naughty list.",
    ),
    exclude_dir: Optional[list[str]] = typer.Option(
        None,
        "--exclude-dir",
        help="Directory subtree to skip entirely; repeatable. Must be an "
             "existing, non-symlink directory strictly inside results_dir, "
             "not reached through a symlinked parent, "
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
    delete_ambiguous: bool = typer.Option(
        False,
        "--delete-ambiguous",
        help="Also delete ambiguous orphans: files that match a rule only "
             "if an unconstrained wildcard spans `/`. By default they are "
             "reported on stderr and never deleted.",
    ),
    directory: Optional[Path] = typer.Option(
        None,
        "--directory",
        "-d",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Working directory the workflow runs in, as `snakemake -d`. "
             "Default: the Snakefile's `workdir:` directive if it has one, "
             "else the Snakefile's directory (its parent for workflow/Snakefile).",
    ),
    allow_locked: bool = typer.Option(
        False,
        "--allow-locked",
        help="Bypass the refusal (dry run or delete) when the working "
             "directory holds Snakemake locks (use only for stale locks from "
             "a crashed run).",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Print orphan counts and sizes per directory instead of listing "
             "each file. Dry run only: refused with --delete/--trash, since a "
             "table cannot show exactly what would be removed.",
    ),
    summary_depth: int = typer.Option(
        2,
        "--summary-depth",
        min=1,
        help="Directory levels below results_dir to group by in --summary.",
    ),
    allow_results_aliases: bool = typer.Option(
        False,
        "--allow-results-aliases",
        help="Bypass the refusal (dry run or delete) when a symlink in the "
             "working directory leads into, onto or above the results "
             "directory. A rule path through such a link names results "
             "files under another spelling, which snakeprune does not "
             "match, so those files would be listed as orphans.",
    ),
    allow_wildcard_absolute_paths: bool = typer.Option(
        False,
        "--allow-wildcard-absolute-paths",
        help="Drop (outputs) or skip (inputs), with a warning, absolute rule "
             "paths outside the results dir that contain any wildcard, "
             "instead of refusing (exit 4). Snakemake wildcards may contain "
             "`/`, so such a path may still name files inside the results "
             "dir (via a wildcard value or a symlink); any it does is then "
             "listed as an orphan and deleted by --delete.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="List at most N deletable orphans on stdout (ambiguous ones on "
             "stderr are not counted). The whole tree is still walked, so "
             "every guard sees all of it. Refused with --delete/--trash.",
    ),
) -> None:
    """Scan a Snakemake project's results directory for orphan files."""

    def log(msg: str) -> None:
        if not quiet:
            typer.echo(msg, err=True)

    if limit is not None and (delete or trash is not None):
        typer.echo(
            "Refusing to delete: --limit shows only part of the orphan list, "
            "and deleting files you were not shown is not allowed. Drop "
            "--limit to delete.",
            err=True,
        )
        raise typer.Exit(code=3)

    deleting = delete or trash is not None
    # A refusal about live-file risk stops a dry run too (exit 3, nothing on
    # stdout): stdout must list exactly what --delete would remove, and a
    # list --delete would refuse is a list someone may pipe into `rm`.
    refusing = "Refusing to delete" if deleting else "Refusing to list orphans"
    if summary and deleting:
        typer.echo(
            "Refusing to delete: --summary shows directory totals, not the "
            "files that would be removed. Run with --summary to survey the "
            "tree, then without it to review the exact list before deleting.",
            err=True,
        )
        raise typer.Exit(code=3)
    if os.path.realpath(results_dir) == os.path.abspath(os.sep):
        typer.echo(
            f"{refusing}: the scanned directory is the filesystem root.",
            err=True,
        )
        raise typer.Exit(code=3)

    if trash is not None:
        if _is_inside(trash, results_dir):
            typer.echo(
                "Refusing to delete: --trash must not be inside the results "
                "directory being scanned. The next scan would report the "
                "trashed files as orphans and --delete would unlink the only "
                "copy. Choose a trash directory outside the results tree.",
                err=True,
            )
            raise typer.Exit(code=3)

    results_name = results_dir_name(results_dir)
    naughty = DEFAULT_NAUGHTY_DIRS | set(naughty_dir or ())
    if results_name in naughty:
        typer.echo(
            f"WARNING: `{results_name}` is a conventional Snakemake "
            f"input/config directory and is unlikely to contain prunable rule "
            f"outputs. Review the orphan list carefully — this may be the wrong "
            f"target.",
            err=True,
        )
        if not allow_naughty_dir:
            typer.echo(
                f"{refusing}: `{results_name}` looks like a "
                f"Snakemake input/config directory. Pass --allow-naughty-dir "
                f"to override.",
                err=True,
            )
            raise typer.Exit(code=3)

    try:
        ignore_file = load_ignore_file(results_dir)
    except IgnoreFileError as exc:
        typer.echo(f"Refusing to scan: {exc}", err=True)
        raise typer.Exit(code=3)
    file_globs: list[str] = []
    if ignore_file is not None:
        file_globs = [glob for _, glob in ignore_file.globs]
        log(
            f"Protecting {ignore_file.entry_count} entr"
            f"{'y' if ignore_file.entry_count == 1 else 'ies'} from {ignore_file.path}."
        )
    ancestor_ignore_files = find_ancestor_ignore_files(results_dir)
    if ancestor_ignore_files:
        typer.echo(
            f"WARNING: {ancestor_ignore_files[0]} protects files in a directory "
            f"above this one, and is not applied when scanning a subdirectory.",
            err=True,
        )
        typer.echo(
            f"{refusing}: scan {ancestor_ignore_files[0].parent} "
            f"itself so its {IGNORE_FILE_NAME} is honoured.",
            err=True,
        )
        raise typer.Exit(code=3)
    ignore = [*(ignore or ()), *file_globs]
    try:
        _compile_globs(ignore)
        exclude_dirs = _resolve_exclude_dirs(results_dir, exclude_dir or ())
        if ignore_file is not None and ignore_file.dirs:
            exclude_dirs += _resolve_exclude_dirs(
                results_dir,
                [d for _, d in ignore_file.dirs],
                labels=[
                    f"{ignore_file.path.name} line {e.line}" for e, _ in ignore_file.dirs
                ],
            )
    except (ExcludeDirError, ValueError) as exc:
        typer.echo(f"Refusing to scan: {exc}", err=True)
        raise typer.Exit(code=3)

    log(f"Loading Snakemake workflow from '{pipeline_dir}'...")
    wildcard_regex = (
        SNAKEMAKE_WILDCARD_REGEX if permissive_wildcards else DEFAULT_WILDCARD_REGEX
    )
    permissive_patterns = None
    permissive_inputs = None
    try:
        resolve_snakefile(pipeline_dir)
        extracted = extract_workflow(
            pipeline_dir, configfiles=tuple(configfile or ()), directory=directory
        )
        workdir = extracted.workdir
        if extracted.workdir_source == "directive":
            log(f"Workflow sets `workdir: {workdir}`; rule paths are read relative to it.")
        elif extracted.workdir_source == "chdir":
            log(f"The Snakefile changes directory to {workdir} while it is parsed; "
                f"rule paths are read relative to it.")
        locks_dir = workdir / ".snakemake" / "locks"
        if not allow_locked and locks_dir.is_dir() and any(locks_dir.iterdir()):
            typer.echo(
                f"{refusing}: `{locks_dir}` is not empty, so a "
                f"Snakemake run appears to be in progress and its partial "
                f"files would be listed as orphans. Wait for it to finish; "
                f"if the locks are stale (a crashed run), clear them with "
                f"`snakemake --unlock` or pass --allow-locked.",
                err=True,
            )
            raise typer.Exit(code=3)
        match_prefix = results_match_prefix(
            pipeline_dir,
            results_dir,
            workdir=None if extracted.workdir_source == "default" else workdir,
        )
        if match_prefix == "" and not allow_basename_mismatch:
            typer.echo(
                f"{refusing}: the scanned directory is the workflow's "
                "working directory, so the Snakefile, config and inputs would "
                "be listed as orphans. Scan the results directory instead, or "
                "pass --allow-basename-mismatch to override.",
                err=True,
            )
            raise typer.Exit(code=3)
        # One resolver for every build, so each distinct prefix is looked up once.
        resolver = PrefixResolver(workdir, results_dir)
        patterns = build_rule_patterns(
            extracted.rules,
            workdir,
            default_wildcard_regex=wildcard_regex,
            on_warning=lambda msg: typer.echo(msg, err=True),
            results_dir=results_dir,
            match_prefix=match_prefix,
            allow_wildcard_absolute=allow_wildcard_absolute_paths,
            resolver=resolver,
        )
        input_patterns = build_input_patterns(
            extracted.rules,
            workdir,
            default_wildcard_regex=wildcard_regex,
            results_dir=results_dir,
            match_prefix=match_prefix,
            allow_wildcard_absolute=allow_wildcard_absolute_paths,
            resolver=resolver,
            on_warning=lambda msg: typer.echo(msg, err=True),
        )
        if not permissive_wildcards:
            # Same rules with Snakemake's own `.+`: an orphan that matches
            # these may be a live output whose wildcard value contains `/`.
            permissive_patterns = build_rule_patterns(
                extracted.rules,
                workdir,
                default_wildcard_regex=SNAKEMAKE_WILDCARD_REGEX,
                results_dir=results_dir,
                match_prefix=match_prefix,
                allow_wildcard_absolute=allow_wildcard_absolute_paths,
                resolver=resolver,
            )
            permissive_inputs = build_input_patterns(
                extracted.rules,
                workdir,
                default_wildcard_regex=SNAKEMAKE_WILDCARD_REGEX,
                results_dir=results_dir,
                match_prefix=match_prefix,
                allow_wildcard_absolute=allow_wildcard_absolute_paths,
                resolver=resolver,
            )
        if not allow_results_aliases:
            aliases = _find_results_aliases(
                resolver, [match_prefix, nfc(results_name) + "/"]
            )
            if aliases:
                typer.echo(
                    f"{refusing}: these symlinks lie below the literal part of "
                    f"a rule path and lead into, onto or above the results "
                    f"directory, so a wildcard value passing through one "
                    f"names a results file under a spelling snakeprune does "
                    f"not match, and the file would be listed as an orphan:",
                    err=True,
                )
                for link, target in aliases[:20]:
                    typer.echo(f"  {link} -> {target}", err=True)
                if len(aliases) > 20:
                    typer.echo(f"  ... and {len(aliases) - 20} more", err=True)
                typer.echo(
                    "Pass --allow-results-aliases if no rule path goes "
                    "through them.",
                    err=True,
                )
                raise typer.Exit(code=3)
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    except (ExtractorError, PatternError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)
    log(
        f"Loaded {len(patterns)} rule path pattern(s) (output, log, benchmark) "
        f"and {len(input_patterns)} input pattern(s)."
    )
    dynamic = [(r.name, r.dynamic_inputs) for r in extracted.rules if r.dynamic_inputs]
    if dynamic:
        total_dynamic = sum(n for _, n in dynamic)
        typer.echo(
            f"Note: {total_dynamic} input(s) in {len(dynamic)} rule(s) are "
            f"functions, so their paths are unknown until the DAG is built; "
            f"a file that only such an input reads, and no rule produces, is "
            f"not protected. List any such file in {IGNORE_FILE_NAME}.",
            err=True,
        )
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
    basename_prefix = nfc(results_name) + "/"
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
        permissive = combine_rule_patterns(permissive_patterns or [])
    except PatternError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)

    log(f"Walking '{results_dir}'...")
    # Orphans go to a temporary file, not a list, so memory stays flat on
    # trees with millions of them.
    spool = OrphanSpool()
    try:
        _scan_and_report(
            spool=spool,
            results_dir=results_dir,
            ignore=ignore,
            follow_symlinks=follow_symlinks,
            exclude_dirs=exclude_dirs,
            limit=limit,
            match_prefix=match_prefix,
            basename_prefix=basename_prefix,
            combined=combined,
            permissive=permissive,
            permissive_patterns=permissive_patterns,
            input_matcher=input_patterns,
            permissive_inputs=permissive_inputs,
            patterns=patterns,
            prefixes=prefixes,
            rule_attribution=rule_attribution,
            orphan_rate_threshold=orphan_rate_threshold,
            allow_high_orphan_rate=allow_high_orphan_rate,
            delete_ambiguous=delete_ambiguous,
            deleting=deleting,
            refusing=refusing,
            summary=summary,
            summary_depth=summary_depth,
            yes=yes,
            allow_symlinks=allow_symlinks,
            trash=trash,
            results_name=results_name,
            log=log,
            ignore_file=ignore_file,
            resolver=resolver,
            allow_results_aliases=allow_results_aliases,
        )
    finally:
        spool.close()


def _scan_and_report(
    *,
    spool: OrphanSpool,
    results_dir: Path,
    ignore,
    follow_symlinks: bool,
    exclude_dirs,
    limit,
    match_prefix: str,
    basename_prefix: str,
    combined,
    permissive,
    permissive_patterns,
    input_matcher,
    permissive_inputs,
    patterns,
    prefixes,
    rule_attribution: bool,
    orphan_rate_threshold: float,
    allow_high_orphan_rate: bool,
    delete_ambiguous: bool,
    deleting: bool,
    refusing: str,
    summary: bool,
    summary_depth: int,
    yes: bool,
    allow_symlinks: bool,
    trash,
    results_name: str,
    log,
    ignore_file=None,
    resolver=None,
    allow_results_aliases: bool = False,
) -> None:
    file_count = 0
    target_prefixes = [match_prefix]
    if basename_prefix != match_prefix:
        target_prefixes.append(basename_prefix)
    explainer = (
        # Outputs first, so a producer rule is named before a consumer.
        AmbiguityExplainer(permissive_patterns, strict=patterns, fallback=permissive_inputs)
        if permissive_patterns
        else None
    )
    dir_summary = DirectorySummary(summary_depth) if summary else None
    walk_stats: dict = {"symlinked_dir_paths": []}
    nested_ignore_files: list[str] = []
    kept_as_input = 0
    n_orphans = 0
    n_ambiguous = 0
    n_symlink_candidates = 0

    def _spool_failed(exc: OSError) -> typer.Exit:
        # The spool is the deletion list; never act on a partial one.
        typer.echo(
            f"Refusing to continue: could not record or read back the orphan "
            f"list in a temporary file ({exc}). Nothing was deleted. Point "
            f"TMPDIR at a filesystem with free space.",
            err=True,
        )
        return typer.Exit(code=3)

    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
        exclude_dirs=tuple(set(exclude_dirs)),
        stats=walk_stats,
        track_globs=[g for _, g in ignore_file.globs] if ignore_file else (),
        watch_name=IGNORE_FILE_NAME,
    ):
        file_count += 1
        if file_count % PROGRESS_INTERVAL == 0:
            log(f"  scanned {file_count} files...")
        # Rule paths are NFC-normalised; some filesystems store names decomposed.
        norm_rel = nfc(rel)
        targets = [tp + norm_rel for tp in target_prefixes]
        if combined is not None and any(combined.match(t) for t in targets):
            continue
        if len(input_matcher) and any(input_matcher.match(t) for t in targets):
            kept_as_input += 1
            continue
        ambiguous = (permissive is not None and any(permissive.match(t) for t in targets)) or (
            permissive_inputs is not None
            and len(permissive_inputs) > 0
            and any(permissive_inputs.match(t) for t in targets)
        )
        n_orphans += 1
        n_ambiguous += ambiguous
        if (
            follow_symlinks
            and not allow_symlinks
            and (delete_ambiguous or not ambiguous)
            and os.path.islink(full_path)
        ):
            n_symlink_candidates += 1
        if ambiguous and explainer is not None:
            explainer.add(rel, targets)
        if dir_summary is not None:
            # Summary mode never deletes or lists files, so nothing is spooled.
            try:
                size = os.lstat(full_path).st_size
            except OSError:
                size = 0
            dir_summary.add(rel, deletable=delete_ambiguous or not ambiguous, size=size)
            continue
        likely = None
        if rule_attribution:
            for t in targets:
                likely = attribute_orphan_to_rule(t, patterns, prefixes)
                if likely:
                    break
        try:
            spool.add(full_path, rel, ambiguous, likely)
        except OSError as exc:
            raise _spool_failed(exc)
    try:
        spool.finish()
    except OSError as exc:
        raise _spool_failed(exc)
    # The walker never yields an entry named like the ignore file (any
    # letter case, any type, even if a symlink or hidden by --ignore). Only
    # the top-level file that was actually loaded is applied, so any other
    # is a protection the user expects but does not have.
    for full, rel in walk_stats.get("watched", ()):
        if "/" not in rel and ignore_file is not None and _same_file(full, ignore_file.path):
            continue
        nested_ignore_files.append(rel)
    log(f"Scanned {file_count} file(s); found {n_orphans} orphan(s).")
    if kept_as_input:
        typer.echo(
            f"Kept {kept_as_input} file(s) only because a rule reads them as "
            f"input (no rule produces them).",
            err=True,
        )

    skipped_dirs = walk_stats.get("skipped_symlinked_dirs", 0)
    # A symlinked directory inside the tree that leads back into, onto or
    # above it lets a wildcard value name a results file by another path.
    dir_links = [(p, os.path.realpath(p)) for p in walk_stats["symlinked_dir_paths"]]
    tree_aliases = [
        (p, t) for p, t in dir_links
        if resolver is not None and resolver.where(t)[0] in ("inside", "ancestor")
    ]
    if skipped_dirs > 0:
        suffix = "y" if skipped_dirs == 1 else "ies"
        typer.echo(
            f"Skipped {skipped_dirs} symlinked subdirector{suffix}; files "
            f"reachable only via those paths were not scanned:",
            err=True,
        )
        for link, target in dir_links[:20]:
            typer.echo(f"  {link} -> {target}", err=True)
        if len(dir_links) > 20:
            typer.echo(f"  ... and {len(dir_links) - 20} more", err=True)

    excluded_dirs = walk_stats.get("excluded_dirs", 0)
    if excluded_dirs > 0:
        typer.echo(
            f"Excluded {excluded_dirs} directory subtree(s) from the scan.",
            err=True,
        )

    unmatched = []
    if ignore_file is not None:
        hits = walk_stats.get("glob_hits", set())
        unmatched = [e for i, (e, _) in enumerate(ignore_file.globs) if i not in hits]
    if unmatched:
        typer.echo(
            f"WARNING: {len(unmatched)} entr{'y' if len(unmatched) == 1 else 'ies'} "
            f"in {ignore_file.path} matched no scanned file. If it is a typo, "
            f"the files it was meant to protect may be listed above. (Files "
            f"inside directories skipped by other entries are not scanned.)",
            err=True,
        )
        for e in unmatched:
            typer.echo(f"  line {e.line}: {e.text}", err=True)
    if nested_ignore_files:
        typer.echo(
            f"WARNING: only the top-level {IGNORE_FILE_NAME} (a regular file) "
            f"is read; these nested ones are not applied, nor is any other "
            f"letter case, symlink or directory of that name:",
            err=True,
        )
        for rel in nested_ignore_files[:20]:
            typer.echo(f"  {rel}", err=True)

    high_rate = False
    # Files kept only as inputs say nothing about whether the workflow's
    # outputs were matched correctly, so they are left out of the rate.
    rated = file_count - kept_as_input
    if rated > 0:
        rate = n_orphans / rated
        if rate > orphan_rate_threshold:
            high_rate = True
            pct = round(rate * 100)
            typer.echo(
                f"WARNING: {pct}% of scanned files are orphans "
                f"({n_orphans} of {rated}"
                f"{' not kept only as inputs' if kept_as_input else ''}). "
                f"This is unusually high and "
                f"usually indicates a config or environment problem rather than "
                f"real cleanup. Review the list carefully before deleting.",
                err=True,
            )

    # The listing and the orphan rate above cover every orphan; only the
    # unambiguous ones go on to deletion unless --delete-ambiguous.
    to_delete = OrphanSelection(spool, include_ambiguous=delete_ambiguous)
    n_delete = len(to_delete)
    # Each refusal below is about a file that may be live or protected, so
    # it stops a dry run as well as --delete, before anything reaches stdout.
    # (Summary mode spools nothing, so count from the walk.)
    n_candidates = n_orphans if delete_ambiguous else n_orphans - n_ambiguous
    refusals: list[str] = []
    if n_candidates:
        if unmatched:
            refusals.append(
                f"{refusing}: {ignore_file.path.name} has entries that "
                f"matched no scanned file, so something you meant to protect "
                f"may be in the orphan list. Fix those lines, or comment out "
                f"any that are simply not needed right now."
            )
        if nested_ignore_files:
            refusals.append(
                f"{refusing}: move the entries of the nested "
                f"{IGNORE_FILE_NAME} file(s) into {results_dir / IGNORE_FILE_NAME}."
            )
        if high_rate and not allow_high_orphan_rate:
            refusals.append(
                f"{refusing}: orphan rate exceeded threshold. Pass "
                f"--allow-high-orphan-rate to override."
            )
        if tree_aliases and not allow_results_aliases:
            listing = "".join(f"\n  {p} -> {t}" for p, t in tree_aliases[:20])
            refusals.append(
                f"{refusing}: these symlinked directories in the results tree "
                f"lead back into, onto or above it, so a rule path whose "
                f"wildcard passes through one names a results file by a path "
                f"snakeprune does not match:{listing}\nPass "
                f"--allow-results-aliases if no rule path goes through them."
            )
        if n_symlink_candidates:
            refusals.append(
                f"{refusing}: {n_symlink_candidates} orphan(s) are symlinks, "
                f"which --delete refuses without --allow-symlinks (it removes "
                f"the link, never its target). Pass --allow-symlinks, or drop "
                f"--follow-symlinks to leave symlinks out."
            )

    if refusals:
        # A refused scan reports nothing about its orphans, the --summary
        # table included: it would describe files that may be live.
        for msg in refusals:
            typer.echo(msg, err=True)
        raise typer.Exit(code=3)
    if dir_summary is not None:
        # stderr, not stdout: stdout is reserved for paths that would be
        # deleted, and a directory column piped into `rm -r` would be a disaster.
        for line in dir_summary.render():
            typer.echo(line, err=True)
    if deleting and n_delete:
        # Every check delete_orphans makes before removing anything, run
        # before the list is printed, so a refused batch is never listed.
        try:
            validate_orphans(
                to_delete,
                allow_symlinks=allow_symlinks,
                trash_dir=trash,
                results_dir_name=results_name if trash is not None else None,
            )
        except (PermissionError, IsADirectoryError, FileExistsError) as exc:
            typer.echo(f"Refusing to delete: {exc}", err=True)
            raise typer.Exit(code=3)
        except OSError as exc:
            raise _spool_failed(exc)
    if dir_summary is None:
        # stdout carries exactly the files --delete would remove, so a held-back
        # ambiguous orphan goes to stderr and cannot be piped into `rm` by accident.
        try:
            # --limit caps only the stdout lines (the deletable orphans);
            # ambiguous ones on stderr are always shown in full.
            listed = 0
            not_listed = 0
            for orphan in spool.iter_orphans():
                held_back = orphan.ambiguous and not delete_ambiguous
                if not held_back and limit is not None and listed >= limit:
                    not_listed += 1
                    continue
                line = str(orphan.path)
                if rule_attribution and orphan.likely_rule:
                    line += f"\t(likely: {orphan.likely_rule})"
                if held_back:
                    typer.echo(f"ambiguous: {line}", err=True)
                else:
                    listed += 1
                    typer.echo(line)
            if not_listed:
                typer.echo(
                    f"--limit {limit}: {not_listed} more deletable orphan(s) "
                    f"not listed.",
                    err=True,
                )
        except OSError as exc:
            raise _spool_failed(exc)

    if n_ambiguous:
        fate = (
            "They will be deleted because --delete-ambiguous was passed."
            if delete_ambiguous
            else "They are never deleted unless you pass --delete-ambiguous."
        )
        where = (
            "counted in the summary's ambiguous column"
            if dir_summary is not None
            else "listed on stderr above unless --delete-ambiguous is set"
        )
        typer.echo(
            f"{n_ambiguous} orphan(s) are ambiguous ({where}): each matches a "
            f"rule if an unconstrained wildcard is allowed to span `/`, as "
            f"Snakemake itself allows, so it may be a live output. {fate} If "
            f"they are live, pass --permissive-wildcards or constrain the "
            f"wildcard.",
            err=True,
        )
        if explainer is not None:
            for line in explainer.render():
                typer.echo(line, err=True)

    if deleting and n_delete:
        total_bytes = 0
        try:
            for o in to_delete:
                try:
                    total_bytes += o.path.stat().st_size
                except OSError:
                    pass
        except OSError as exc:
            raise _spool_failed(exc)
        what = f"{n_delete} file(s) ({human_size(total_bytes)})"
        typer.echo(
            f"About to move {what} to the trash at '{trash}'."
            if trash is not None
            else f"About to delete {what}.",
            err=True,
        )
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
                to_delete,
                allow_symlinks=allow_symlinks,
                trash_dir=trash,
                results_dir_name=results_name if trash is not None else None,
            )
        except DeletionFailed as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=5)
        except (PermissionError, IsADirectoryError, FileExistsError) as exc:
            typer.echo(f"Refusing to delete: {exc}", err=True)
            raise typer.Exit(code=3)
        except OSError as exc:
            # delete_orphans reports any failure once removal has begun as
            # DeletionFailed, so a bare OSError comes from validation (reading
            # the list back, or checking an orphan or the trash directory).
            typer.echo(
                f"Refusing to delete: checking the deletion batch failed "
                f"({exc}). Nothing was deleted.",
                err=True,
            )
            raise typer.Exit(code=3)
