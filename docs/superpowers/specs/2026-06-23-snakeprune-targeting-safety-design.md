# snakeprune Targeting Safety — Naughty Dirs & Exclude Dirs — Design

**Date:** 2026-06-23
**Status:** Draft, awaiting user review

## Problem

Both of the issues here are about the *target* the user points snakeprune at, before any rule matching happens.

1. **Pointing at a conventional input/config directory.** Snakemake projects have well-known directories that hold *inputs*, *config*, or *workflow code* rather than rule outputs: `resources/`, `config/`, `profile/`, `workflow/`, `.snakemake/`. If a user runs `snakeprune scan <pipeline> resources` (especially `resources/`, which can hold large downloaded reference data), few or no rules write there, so most files look like orphans — and `--delete` would destroy real input data. The existing **basename-mismatch** refusal catches this *only when no rule writes under that basename*; it does **not** fire when a download rule legitimately writes under `resources/`, leaving a real gap.

2. **Wanting to keep a known subtree.** A user may legitimately want to scan `results/` but protect a subtree they know is fine, e.g. `results/old_results_to_keep/`. This is already *possible* via `--ignore "old_results_to_keep/**"`, but that path is applied per-file (`walker.iter_results_files`), so the excluded subtree is still fully `scandir`-walked and then discarded — wasted I/O on large trees — and the glob has a footgun (the `/**` suffix is mandatory, and the glob is relative to `results_dir`, not the literal `results/...` path the user pictures).

This spec adds two targeting-safety features: a **naughty-directory guard** (warn always, refuse on delete) and a dedicated **`--exclude-dir`** option that prunes whole subtrees from the walk.

## Approach

Both features are cheap, target-only checks that need no rule information, so they run early in `cli.scan`:

- The **naughty-dir guard** runs *first thing* in `scan()`, before the workflow subprocess is even spawned — fail fast, and never walk a huge `resources/` tree just to refuse afterwards.
- **`--exclude-dir`** is threaded into the walker so excluded directories are never descended into (true walk-pruning), not merely filtered after the fact.

Both reuse existing conventions: exit code 3 for the refusal (consistent with the other safety gates), per-feature override flag, and a one-line stderr tally for visibility (mirroring the skipped-symlinked-dirs line).

## Components

### Naughty-directory guard (in `cli.scan`)

A module-level constant in `cli.py`:

```python
DEFAULT_NAUGHTY_DIRS = frozenset({"resources", "config", "profile", "workflow", ".snakemake"})
```

(`logs` and `benchmarks` are deliberately excluded: Snakemake's `log:` and `benchmark:` directives mean those dirs usually hold rule-associated files, so orphan logs are a legitimate prune target — warning on them would be noise.)

New flags on `scan`:

```
--naughty-dir NAME       Extra directory basename to treat as suspicious; repeatable
--allow-naughty-dir      Bypass the delete/trash refusal for a naughty target dir
```

Behaviour, evaluated at the very top of `scan()` (before `find_rule_patterns`):

1. Build the effective set: `DEFAULT_NAUGHTY_DIRS | set(naughty_dir or ())`.
2. If `results_dir.name` is in that set (case-sensitive exact match):
   - **Always** print a loud warning to stderr naming the directory and why it is suspicious, e.g.:

     > WARNING: `resources` is a conventional Snakemake input/config directory and is unlikely to contain prunable rule outputs. Review the orphan list carefully — this may be the wrong target.

   - If deletion is requested (`--delete` or `--trash`) **and** `--allow-naughty-dir` is not set → print a refusal and exit 3 **immediately** (before loading the workflow):

     > Refusing to delete: `resources` looks like a Snakemake input/config directory. Pass `--allow-naughty-dir` to override.

   - Otherwise (dry-run, or deletion with the override) → continue normally.

Case-sensitive exact match on `results_dir.name`: Snakemake conventions are lowercase, and exact matching avoids surprising partial hits.

### `--exclude-dir` (in `cli.scan` + `walker.iter_results_files`)

New flag on `scan`:

```
--exclude-dir PATH       Directory subtree to skip entirely; repeatable.
                         Relative paths are resolved against results_dir.
```

CLI resolution (in `scan`, before the walk):

- For each `--exclude-dir` value: if the path is absolute, resolve it as-is; otherwise resolve it against `results_dir` (so `--exclude-dir old_results_to_keep` → `<results_dir>/old_results_to_keep`).
- Normalise each to an absolute path string (via `os.path.abspath`) and collect into a set passed to the walker. No requirement that the dir exists — a non-existent exclude path simply never matches (no error, no note).

Walker change — `iter_results_files` gains `exclude_dirs: Iterable[str] = ()`:

- Normalise to a set of absolute path strings once on entry.
- In the directory-descent branch (currently `walker.py:80-81`), before pushing a subdirectory onto the stack, compare its absolute path against the exclude set. On a match, **skip the push** (do not descend) and, if `stats` is provided, increment `stats["excluded_dirs"]`.
- Files directly listed (not directories) are unaffected.

The comparison uses `os.path.abspath(entry.path)`; the base directory itself is the walk root and is never a descent candidate, so excluding `results_dir` itself is a no-op edge case (acceptable — if you exclude the whole target there is simply nothing to scan).

CLI surfacing after the walk (mirroring the skipped-symlinked-dirs line):

> Excluded N directory subtree(s) from the scan.

`exclude_dirs` defaults to `()` so existing callers and tests are unaffected.

### Interaction with existing checks

- The naughty-dir guard is independent of and **earlier** than the empty-rule and basename-mismatch refusals. A naughty target with `--delete` exits 3 before the workflow even loads.
- `--exclude-dir` reduces the set of scanned files, so excluded files do **not** count toward the high-orphan-rate denominator — correct, since they were never candidates.
- `--exclude-dir` and `--ignore` coexist; `--ignore` remains for file-level/glob exclusion, `--exclude-dir` for whole-subtree walk-pruning.

## Behaviour specification

- **`results_dir.name` in naughty set, dry-run** → loud warning on stderr, exit 0, orphans listed normally.
- **`results_dir.name` in naughty set, `--delete`/`--trash`, no `--allow-naughty-dir`** → warning + refusal, exit 3, workflow not loaded, nothing deleted.
- **`results_dir.name` in naughty set, `--delete`, with `--allow-naughty-dir`** → warning printed, proceeds into the normal delete flow.
- **`--naughty-dir custom_inputs`** → a target dir named `custom_inputs` triggers the guard exactly like a built-in entry.
- **Non-naughty target dir** → no warning, behaviour unchanged.
- **`--exclude-dir old_results_to_keep`** → `<results_dir>/old_results_to_keep` is never descended into; its files are absent from the scan and the orphan list.
- **`--exclude-dir` with an absolute path** → resolved as-is and matched against directory paths during the walk.
- **`--exclude-dir` pointing at a non-existent dir** → no error, simply never matches.
- **Excluded directories** → counted and surfaced as a one-line stderr summary if any were excluded.

**Backwards-compatibility note.** Purely additive. No existing flag changes meaning; default behaviour (no `--naughty-dir`/`--exclude-dir`) is unchanged except that targeting one of the five built-in naughty basenames now prints a warning (and refuses under `--delete` without the override). That last point is a deliberate, conservative UX change consistent with the project's other safety gates.

## Testing

New tests in existing files (no new modules):

- `test_cli.py`:
  - dry-run targeting a `resources/` dir → warning on stderr, exit 0, orphans still listed
  - `--delete` targeting a `config/` dir → refusal, exit 3, nothing deleted, workflow-load message absent
  - `--delete --allow-naughty-dir` targeting `config/` → proceeds into delete flow
  - `--naughty-dir custom_inputs` makes a `custom_inputs/` target trigger the guard
  - a normal target dir (e.g. `results/`) → no naughty warning
  - `--exclude-dir sub` → files under `<results_dir>/sub` are absent from the orphan list
  - `--exclude-dir` with an absolute path works
  - excluded-dir tally line appears when an exclusion took effect
- `test_walker.py`:
  - `exclude_dirs={...}` prevents descent into the matching subtree (its files are not yielded)
  - `exclude_dirs` with a path not present in the tree is a no-op
  - `stats["excluded_dirs"]` counts matches; default (no kwarg) behaviour unchanged

Interactive/non-TTY delete behaviour is already covered by the existing suite; the naughty-dir `--delete` tests use the same `CliRunner` patterns (the refusal fires before any prompt, so no TTY interaction is needed).

## README updates

Under **Safety and limitations → Built-in refusals**, add a bullet:

- **Naughty target directories.** Targeting a conventional Snakemake input/config dir (`resources`, `config`, `profile`, `workflow`, `.snakemake`) prints a warning and, under `--delete`/`--trash`, refuses unless `--allow-naughty-dir` is passed. Extend the list with `--naughty-dir NAME` (repeatable).

Under **Usage**, add an `--exclude-dir` example, and document it near the existing `--ignore` example, noting the distinction (subtree walk-pruning vs file-level glob).

## Out of scope

- Making `--ignore` itself prune the walk (kept simple; `--exclude-dir` is the walk-pruning path, `--ignore` stays glob/file-level).
- Configurable case-insensitive matching for naughty dirs (exact lowercase match covers the conventions).
- Persisting a naughty list in a config file (the `--naughty-dir` flag plus the built-in default is enough for now).
