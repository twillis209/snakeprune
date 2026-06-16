# snakeprune Design

**Date:** 2026-06-16
**Status:** Draft, awaiting user review

## Problem

Snakemake pipelines accumulate orphan files in `results/` over their lifetime. When a new wildcard is added to an output path (e.g., `{pc_orth}` slotted between two existing segments), every previously-generated file at the old path becomes invisible to Snakemake's DAG: still on disk, but unreachable from any current rule. Over months of development this accumulates to tens of thousands of obsolete files, consuming space and stressing HPC user file-count limits.

`snakeprune` detects these orphans by ingesting the pipeline's rule definitions and matching every file in `results/` against the union of rule output patterns. Files that match no current pattern are reported as orphans, optionally deletable behind an explicit flag.

## Approach

Use Snakemake's Python API as the source of truth for "valid" output paths. Load the workflow programmatically, iterate over `workflow.rules`, extract each rule's `output` patterns, build a regex per pattern by substituting `{wildcards}` with their constraints (rule-local first, then workflow-global, falling back to `[^/]+` if no constraint is defined). Walk the project's `results/` directory and mark any file matching at least one regex as live; the rest are orphans.

This is more robust than text-parsing `.smk` files because it correctly handles `include:` chains, `multiext()`, dynamic output specifications, and Snakemake's internal wildcard-constraint resolution.

## Components

### `patterns.py` — Pattern extraction

Given a Snakefile path, returns a list of `(rule_name, compiled_regex)` tuples covering every output spec in the workflow.

- **Inputs:** path to the project's main Snakefile (Snakemake handles `include:` resolution).
- **Output:** `list[tuple[str, re.Pattern]]`.
- **Internals:** loads the workflow via Snakemake's API, iterates over `workflow.rules`, expands each rule's output list (`multiext()` outputs expand to multiple distinct patterns), then for each output string substitutes every `{wildcard}` with its regex body. The regex body comes from rule-local `wildcard_constraints` if present, else workflow-global `wildcard_constraints`, else the default `[^/]+`.

### `walker.py` — Directory walk and matching

Given a `results/` directory and a list of patterns, returns the set of orphan files.

- **Inputs:** results directory path, pattern list, optional ignore-glob list.
- **Output:** `list[OrphanFile]` where `OrphanFile` carries the path and, optionally, the best-guess rule attribution.
- **Internals:** `Path.rglob("*")`, skip directories and (by default) symlinks. For each file, test against all compiled regexes; if no match, it's an orphan. Optional rule-attribution mode does a coarser match (e.g., longest common prefix) to suggest which rule's output the orphan resembled.

### `delete.py` — Deletion (behind explicit flag)

Given a list of orphans, unlinks them one at a time. Refuses to operate on symlinks unless `--allow-symlinks` is passed. Never recurses into directories — only deletes regular files. Always prints what's being deleted to stderr.

### `cli.py` — Typer CLI

Single subcommand for MVP:

```
snakeprune scan <snakefile> <results_dir>
    [--delete]                # actually unlink orphans; default is dry-run
    [--rule-attribution]      # show best-guess rule per orphan
    [--ignore PATTERN]        # repeatable; glob patterns to skip
    [--follow-symlinks]       # default skip
    [--quiet]                 # don't print live files
```

Default behaviour is dry-run: list orphans, take no destructive action.

## Behaviour specification

- **Live file**: any file under `results/` whose path matches at least one rule's output regex.
- **Orphan**: any file under `results/` that is not live.
- **Ignored**: files matching any `--ignore` glob (treated as neither live nor orphan; not reported).
- **Symlinks**: skipped by default (not followed and not reported).
- **Directories**: never reported as orphans (we only operate on regular files).
- **Wildcards with no constraint**: fall back to `[^/]+`, matching one or more non-slash characters. Matches Snakemake's own default.

## Edge cases

| Case | Handling |
|------|----------|
| `multiext()` output spec | Expanded into N distinct patterns (one per file extension). |
| `directory(...)` output | The directory itself is matched; files inside it are *not* automatically considered live unless they match the directory pattern + further wildcards. |
| Dynamic outputs via lambda or function | Pattern extraction may not see these; reported as a warning, those rules are skipped. |
| Wildcard not declared in any `wildcard_constraints` | Falls back to `[^/]+`. |
| Files in `results/` but produced manually (logs, notes) | User adds `--ignore` patterns to exclude them. |
| Empty `results/` directory | Returns empty orphan list, exits successfully. |

## Non-goals (MVP)

- Parallel directory traversal (a single thread with `rglob` is fast enough for millions of files).
- Progress bars.
- Multi-project / multi-Snakefile scanning (one workflow at a time).
- Restoration of orphans (we only delete, never resurrect).
- Detecting orphans outside `results/`.

## Testing

Synthetic Snakefiles in `tests/fixtures/`, paired with synthetic `results/` directory trees. Each test:

1. Builds a minimal Snakefile with a known set of output patterns.
2. Pre-populates a `results/` tree with both live and orphan files.
3. Asserts that `find_orphans` returns exactly the expected orphan set.

Edge cases to cover: `multiext`, missing wildcard constraints, nested includes, `directory(...)` outputs, symlinks, ignored patterns.

## Performance

For a `results/` tree with N files and a workflow with R rule output patterns, the cost is O(N · R) regex matches per scan. Compiled regexes are reused; expected throughput on the order of 10^6 files/minute on a modern laptop. No need for parallelism in MVP.

## Future work (out of MVP scope)

- Multi-Snakefile workflows (different pipelines sharing one `results/`).
- Interactive deletion mode (confirm each orphan).
- Stale-input detection: files that *are* live but are downstream of inputs that no longer exist.
- Disk-usage summary by rule (largest orphan classes).
