# Usage

```bash
# Dry-run: list orphans, take no action
snakeprune scan path/to/pipeline path/to/results

# Survey a big tree: orphan counts and sizes per directory, no per-file list
snakeprune scan path/to/pipeline path/to/results --summary --summary-depth 3

# With rule attribution (best-guess rule per orphan)
snakeprune scan path/to/pipeline path/to/results --rule-attribution

# Skip intentional manual files (repeatable; supports prefix/** for directories)
snakeprune scan path/to/pipeline path/to/results --ignore "notes/**" --ignore "*.log"

# Skip an entire existing subtree from the walk (faster than --ignore for big dirs)
snakeprune scan path/to/pipeline path/to/results --exclude-dir old_results_to_keep

# Show only the first 20 deletable orphans (ambiguous ones on stderr are not
# counted; the whole tree is still walked and checked)
snakeprune scan path/to/pipeline path/to/results --limit 20

# Let unconstrained wildcards span '/' (Snakemake's own default); fewer orphans
snakeprune scan path/to/pipeline path/to/results --permissive-wildcards

# Actually delete (refuses symlinks unless --allow-symlinks)
snakeprune scan path/to/pipeline path/to/results --delete

# Non-interactive delete (for scripts)
snakeprune scan path/to/pipeline path/to/results --delete --yes

# Reversible delete: move orphans into a trash directory instead of unlinking
snakeprune scan path/to/pipeline path/to/results --trash path/to/trash

# Pass config to the workflow loader so config-gated rules are visible
snakeprune scan path/to/pipeline path/to/results --configfile path/to/config.yaml

# The workflow was run with `snakemake -d path/to/rundir`
snakeprune scan path/to/pipeline path/to/rundir/results --directory path/to/rundir
```

`path/to/pipeline` is the directory that holds either `Snakefile` or `workflow/Snakefile`; for the recommended `workflow/Snakefile` layout that is the project root. `snakeprune` looks for `<pipeline>/Snakefile` first, then `<pipeline>/workflow/Snakefile`.

Run `snakeprune scan --help` for the full list of options.

## Notes on options

- **`--exclude-dir` vs `--ignore`.** `--exclude-dir DIR` prunes a whole subtree from the walk (the directory is never descended into), which is faster than `--ignore "DIR/**"` for large directories you want to protect. `--ignore` remains for file-level glob filtering. Relative `--exclude-dir` paths resolve against the results directory, not the current directory; a value that cannot be honoured is refused (see *Bad `--exclude-dir`* in [Safety and limitations](safety.md#built-in-refusals)).
- **`--ignore` globs.** `*` crosses `/`, and `**/` also matches zero directories, so `--ignore "**/*.bai"` covers `.bai` files at the top level as well as nested ones. A glob may use `**/` in at most three separate places; more is refused (exit 3) because matching would become very slow.
- **`print()` in Snakefiles is fine.** Anything the workflow (or its child processes) writes to stdout is diverted to stderr so it cannot corrupt the extractor's JSON.

## Protecting files permanently: `.snakepruneignore`

Put a `.snakepruneignore` file at the top of the results directory to record what `snakeprune` must never list or delete, so keeping a snapshot does not depend on remembering `--exclude-dir` on every run. Every scan of that directory applies it, alongside any `--ignore` / `--exclude-dir` flags.

```
# Snapshots and hand-made workups: keep
_old_precorr_2026-11-04/
ad_hoc/
# Every top-level directory starting with an underscore
_*/
# Individual files
*.ipynb
```

- Entries are relative to the results directory and anchored there (unlike `.gitignore`, `notes/` means `results/notes/`, not any `notes/`).
- An entry naming an existing directory (`ad_hoc/`, `ad_hoc`, even `run[1]/`) protects that subtree, which is skipped entirely, exactly like `--exclude-dir`. A `name/` entry without glob characters must exist: a misspelt directory would protect nothing, so the scan is refused (exit 3) with the line number.
- `pattern/` with glob characters (`_*/`) protects everything under any directory it matches.
- Anything else is a glob with `--ignore` semantics.
- A glob that matches no file is reported and the run is refused (exit 3, a dry run included) until you fix or remove the line, because the files it was meant to protect may be in the orphan list.
- `!` negation, absolute paths and `..` are refused rather than guessed at.
- The name is matched in any letter case (`.SnakepruneIgnore` counts), since a case-insensitive filesystem such as macOS's opens it either way.
- Only the top-level file is read (a regular file; the one actually loaded). The file itself, and anything else of that name anywhere in the tree (a nested file, another letter case, a symlink, a directory, even one hidden by an `--ignore` glob), is never treated as an orphan, but anything other than the loaded file is reported and refuses the run (exit 3, a dry run included) until its entries are moved to the top-level file.
- Scanning a subdirectory of a tree that has a `.snakepruneignore` above it bypasses that file, so the run is refused (a dry run included); scan the directory that holds the file. Directories above the results directory are checked both as spelled and after resolving symlinks, so reaching it through a symlinked directory does not hide the file.

## Deletion flow

- Default behaviour without `--delete` or `--trash` is dry-run: list orphans and take no action.
- `--delete` shows the count and total size of the files it is about to remove and prompts `[y/N]` before unlinking.
- `--yes` skips the prompt; required when stdin is not a TTY (e.g. in scripts).
- `--trash DIR` moves each orphan to `DIR/<results-dir-name>/<rel-path>` instead of unlinking, so deletions are reversible. Passing `--trash` implies deletion — you do not also need `--delete`.
- If a deletion batch fails part-way (permissions, disk full, file vanished, or the orphan list cannot be read back), `snakeprune` stops, prints `Deleted N of M file(s) before failing ...` (`Moved N of M file(s) to trash ...` with `--trash`), and exits 5. Files listed under `deleting:` / `moving to trash:` before the failure are gone or moved; the rest were not touched. A failure while the batch is still being checked, before anything is removed, exits 3 and says nothing was deleted.

## Exit codes

| code | meaning |
|---|---|
| 0 | success (a dry run exits 0 whether or not it finds orphans, unless a safety guard refuses), or you answered `n` at the prompt |
| 2 | no Snakefile found |
| 3 | refused by a safety guard before anything was deleted (or, in a dry run, listed) |
| 4 | the workflow could not be loaded, its working directory could not be established, or a rule path that may lie in the results tree could not be used |
| 5 | deletion started but did not complete (message says how many were removed) |

See also [Safety and limitations](safety.md) and [How it works](how-it-works.md).
