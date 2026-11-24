# snakeprune

Find 'orphan' files in a `snakemake` project's `results/` directory and delete them.

## Motivation

Experimentation in bioinformatics typically entails exploration of an ever-expanding number of parameters, flags, procedures, etc. Encoding these as wildcards in the paths to output files in your `snakemake` rules leads to a growing tree of redundant files under `results`, e.g.
* `results/{sample}/{replicate}/output.txt` becomes
* `results/{sample}/{replicate}/{normalisation}/output.txt` becomes
* `results/{sample}/{replicate}/{normalisation}/{count_threshold}/output.txt`

...and so on. If you settle on the third of those output paths, the first two files will be left in the `results` directory tree. When working with very large numbers of files (say, one per human gene), multiple iterations clutter up your filesystem over time and stir the bloodlust of your HPC team. To my knowledge `snakemake` provides no way of cleaning these files up.

`snakeprune` ingests the workflow by invoking a small extractor script via subprocess in the workflow's own Python environment, builds a regex pattern per file a rule produces (its `output`, `log`, and `benchmark` paths), walks the project's `results/` tree, and reports files that match no pattern. `snakeprune` can delete these files or move them to a designated trash directory whilst preserving the same output path from `results` onwards.

As with any program intended to delete files: USE AT YOUR OWN RISK!

## Install

`snakeprune` itself is a small pure-Python CLI. It is not on PyPI; install it straight from GitHub. The recommended way is [`pipx`](https://pipx.pypa.io/), so it lives in its own isolated env and works against any workflow:

```bash
pipx install git+https://github.com/twillis209/snakeprune.git
```

Or, to install from a clone:

```bash
git clone https://github.com/twillis209/snakeprune.git
cd snakeprune
pip install -e .
```

`snakeprune` needs Python 3.12 or later. It calls into the workflow's own Python environment via subprocess, so `snakemake` (version 8 or later) must be importable in *that* environment — but it does not need to live in the same env as `snakeprune`.

## Usage

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

### Notes on options

- **`--exclude-dir` vs `--ignore`.** `--exclude-dir DIR` prunes a whole subtree from the walk (the directory is never descended into), which is faster than `--ignore "DIR/**"` for large directories you want to protect. `--ignore` remains for file-level glob filtering. Relative `--exclude-dir` paths resolve against the results directory, not the current directory; a value that cannot be honoured is refused (see *Bad `--exclude-dir`* below).
- **`--ignore` globs.** `*` crosses `/`, and `**/` also matches zero directories, so `--ignore "**/*.bai"` covers `.bai` files at the top level as well as nested ones. A glob may use `**/` in at most three separate places; more is refused (exit 3) because matching would become very slow.
- **`print()` in Snakefiles is fine.** Anything the workflow (or its child processes) writes to stdout is diverted to stderr so it cannot corrupt the extractor's JSON.

### Protecting files permanently: `.snakepruneignore`

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

## Safety and limitations

`snakeprune` is destructive at the user's request, so the CLI tries hard to refuse rather than do the wrong thing.

### Before you delete

The guards below catch a wrong target or a broken workflow load. They cannot catch a file that is live but that no rule pattern describes. Check the dry-run list for these five cases, each explained under [Limitations](#limitations):

- **A wildcard value containing `/`** (e.g. `sample=batch1/s1`) makes a live file look like an orphan. Such files are reported on stderr as `ambiguous: <path>` and are never deleted unless you pass `--delete-ambiguous`; if they are live, pass `--permissive-wildcards` or constrain the wildcard. A closing report attributes each ambiguous file to the first rule (in workflow order) that claims it and names every unconstrained wildcard in its path with an example of the values that matched. If the files are stale, constrain *all* of the listed wildcards: with two adjacent ones such as `{a}/{b}`, constraining only `a` leaves `b` free to absorb the extra directory.
- **Files a tool writes but no rule declares** (`.bai`, `.tbi`, `.fai`, tool-written logs, RFMix's `.rfmix.Q` beside its declared outputs) are reported as orphans. List them in `.snakepruneignore`, or declare them as outputs.
- **Files only an input function reads.** Every static rule input is live, even if no rule produces it (a frozen source copied in by hand, say). But an input given as a function (`input: lambda w: ...`) has no path until the DAG is built, so a file that only such a function reads is not protected; the scan prints how many such inputs there are.
- **Rules that only exist under a particular config** are invisible unless you pass that config with `--configfile`, and their outputs are then reported as orphans.
- **A running workflow** has temporary and partial files on disk that match no pattern. The scan is refused while the working directory's `.snakemake/locks` is non-empty, but a run started from a directory `snakeprune` does not know about is not detected.

### Built-in refusals

Every refusal that would stop `--delete` / `--trash` because a listed file might be live or protected stops a dry run too: exit 3, nothing on stdout, the reason on stderr. So a dry run's stdout is always exactly what `--delete` would remove, never a list `--delete` would refuse. Where a refusal has an override flag, the flag works in a dry run as well. Refusals about the mechanics of deleting (`--limit`, `--summary`, trash placement, the confirmation prompt, symlinks without `--allow-symlinks`) apply only to `--delete` / `--trash`. With `--summary`, the table is still printed on stderr before the refusal.

- **Empty rule list.** If the workflow loads but produces zero path patterns (e.g. all rules are gated behind config that wasn't passed), `snakeprune` refuses to scan rather than report every file as an orphan. Override with `--allow-empty-rules`.
- **Results-dir / rule-prefix mismatch.** If no rule writes under the results directory's path relative to the workflow's working directory, nor under its basename (see below), every file would look like an orphan. `snakeprune` refuses and surfaces the prefixes the rules actually use. Override with `--allow-basename-mismatch`.
- **Scanning the workflow directory itself.** If the scanned directory is the workflow's working directory, the scan is refused (exit 3) because the Snakefile, config and inputs would be listed as orphans. Override with `--allow-basename-mismatch`.
- **Bad `--exclude-dir`.** Each value must be an existing, non-symlink directory strictly inside the results directory, spelled exactly as on disk (letter case and Unicode form matter, even on case-insensitive filesystems), and not reached through a symlinked parent directory. Otherwise `snakeprune` exits 3 before scanning (a typo would silently protect nothing).
- **Workflow locked.** The scan is refused (exit 3) while `<working directory>/.snakemake/locks` contains anything, because a Snakemake run appears to be in progress. For stale locks left by a crashed run, clear them with `snakemake --unlock` or pass `--allow-locked`.
- **`--summary` with deletion.** `--summary` replaces the per-file list with per-directory totals on stderr (stdout stays empty, so nothing can be piped into `rm`), so it is refused (exit 3) together with `--delete` / `--trash`: you would be deleting files you never saw listed.
- **Incomplete orphan list.** Orphans are recorded in a temporary file (in `$TMPDIR`) during the walk rather than held in memory. If that file cannot be written in full (e.g. the disk is full), or cannot be read back while the deletion batch is being checked, the run stops with exit 3 before anything is deleted.
- **Filesystem root.** The scan is refused outright when the scanned directory is `/`.
- **Trash target blocked by a file.** `--trash DIR` is refused up front (exit 3) if the trash directory itself or any directory level between it and a target already exists as a non-directory, so nothing is moved before the failure.
- **High orphan rate.** When more than `--orphan-rate-threshold` (default 0.5) of scanned files would be orphans, `snakeprune` prints a loud warning and refuses unless `--allow-high-orphan-rate` is also passed. Files kept only because a rule reads them as input are left out of the rate (it is orphans over orphans plus files a rule produces), so a large set of frozen inputs cannot hide a broken match. Pass `--orphan-rate-threshold 1.0` to disable the check entirely.
- **Naughty target directories.** Targeting a conventional Snakemake input/config directory (`resources`, `config`, `profile`, `workflow`, `.snakemake`) prints a warning and refuses unless `--allow-naughty-dir` is passed. The check fires before the workflow is even loaded, and especially guards against pointing at `resources/` (downloaded reference data). Extend the list for a run with `--naughty-dir NAME` (repeatable). The directory's real name is used, so spelling the target as `.` or `..` does not avoid the check.
- **Trash inside the results tree.** `--trash DIR` is refused if `DIR` is the results directory or anywhere beneath it (compared by file identity, so a symlink or a different letter case does not hide it), because the next scan would report the trashed files as orphans.
- **Unparseable rule paths.** If a rule output, log or benchmark path contains brace syntax `snakeprune` cannot read, or a `..` that follows a wildcard segment (e.g. `results/{s}/../x.txt`, whose target depends on the wildcard value), it exits 4 rather than guess — an unparsed pattern would mark every file of that rule as an orphan. Other oddities (`./`, `//`, a trailing `/`, `seg/..` after a literal segment) are normalised, and a leading `..` is resolved against the workflow's working directory.
- **Unusable input paths.** Inputs get the same treatment wherever they may lie inside the results tree: one with unreadable braces or a `..` after a wildcard, or one naming the working directory itself, exits 4 with the rule and input named. Relative inputs that plainly lie elsewhere (`resources/...`) can never be deleted and are skipped silently; absolute ones are handled as below.
- **Absolute rule paths outside the results directory.** Absolute paths (outputs and inputs) under the scanned results directory are relativised and work normally, including when the rule spells the path through a symlink (`/tmp` vs `/private/tmp`, a symlinked `/scratch`), or in another letter case on a filesystem that treats the two spellings as one directory. Otherwise:
  - A fully literal path, or one whose only wildcard is in its final component (`/data/raw/{s}.fq`), cannot name a scanned file: an output is dropped with a warning (shown even under `--quiet`), an input skipped silently. The exception is a `directory()` output or an input that may be the results directory or one of its ancestors, which exits 4.
  - A path with a wildcard in a directory part (`/data/{proj}/raw/x.fq`, `/base/p{x}/results/live.dat`) might name files inside the results directory for some wildcard value or through a symlink, which `snakeprune` cannot rule out, so it exits 4 naming the rule and path. Pass `--allow-wildcard-absolute-paths` to drop such outputs and skip such inputs with a warning instead; only do so if they never lead into the results directory, since any live file they name there would then be listed as an orphan and deleted by `--delete`.

### Deletion flow

- Default behaviour without `--delete` or `--trash` is dry-run: list orphans and take no action.
- `--delete` shows a summary (count + total bytes) and prompts `[y/N]` before unlinking.
- `--yes` skips the prompt; required when stdin is not a TTY (e.g. in scripts).
- `--trash DIR` moves each orphan to `DIR/<results-dir-name>/<rel-path>` instead of unlinking, so deletions are reversible. Passing `--trash` implies deletion — you do not also need `--delete`.
- If a deletion batch fails part-way (permissions, disk full, file vanished, or the orphan list cannot be read back), `snakeprune` stops, prints `Deleted N of M file(s) before failing ...` (`Moved N of M file(s) to trash ...` with `--trash`), and exits 5. Files listed under `deleting:` / `moving to trash:` before the failure are gone or moved; the rest were not touched. A failure while the batch is still being checked, before anything is removed, exits 3 and says nothing was deleted.

### Exit codes

| code | meaning |
|---|---|
| 0 | success (a dry run exits 0 whether or not it finds orphans, unless a safety guard refuses), or you answered `n` at the prompt |
| 2 | no Snakefile found |
| 3 | refused by a safety guard before anything was deleted (or, in a dry run, listed) |
| 4 | the workflow could not be loaded, its working directory could not be established, or a rule path that may lie in the results tree could not be used |
| 5 | deletion started but did not complete (message says how many were removed) |

### Recommended first-use workflow

1. Run a plain `snakeprune scan <pipeline> <results>` and eyeball the orphan list. On a large tree, start with `--summary` to see which directories the orphans are in, then list anything you want to keep in `results/.snakepruneignore`.
2. If the list looks right, re-run with `--trash some/staging/dir` to move (not unlink) the orphans.
3. After a few days of confidence that nothing's missing, delete the trash directory.

### Limitations

- **Nested results directories.** Scanned files are matched against rule patterns by the results directory's path relative to the workflow's working directory e.g. `results/qc/a.txt` for `path/to/results/qc`. The working directory is, in order: `--directory DIR` (use it if you ran `snakemake -d DIR`), the Snakefile's own `workdir:` directive, then the Snakefile's directory (its parent for `workflow/Snakefile`). The Snakefile is parsed as `snakemake` would parse it: from that default directory, with each `workdir:` directive changing directory as it runs, so parse-time code such as `os.path.abspath("results/{s}.txt")` resolves where Snakemake's does; the working directory used is wherever parsing leaves it. A `workdir:` naming a directory that does not exist is refused (exit 4), since Snakemake would create it and `snakeprune` creates nothing. `snakeprune` cannot know the directory you actually ran `snakemake` from, so a file is also live if it matches under the results directory's basename (e.g. `results/qc/a.txt` and `qc/a.txt` for the example above). If the results directory is outside the working directory, only the basename is used. Scanning a sub-directory such as `results/qc` when the rules' literal prefix is shorter (e.g. `results/{d}/x.txt`) therefore trips the "No rule writes under" guard and needs `--allow-basename-mismatch`.
- **Unconstrained wildcards are stricter than Snakemake's.** Snakemake lets an unconstrained `{wildcard}` match `.+`, which spans `/`. `snakeprune` deliberately uses `[^/]+` instead: with `.+`, a rule writing `results/{sample}/{replicate}/out.txt` would also claim `results/A/B/norm/out.txt`, hiding exactly the stale deeper layouts this tool exists to find. The cost is that a file Snakemake produced with a `/` inside a wildcard value (e.g. `sample=batch1/s1`) is reported as an orphan. `snakeprune` cannot tell that case from a stale deeper layout, so any orphan that *would* match a rule under `.+` is reported on stderr as `ambiguous: <path>` instead of on stdout, and is held back from `--delete` / `--trash`. Stdout therefore lists exactly the files that would be deleted (and nothing at all when `--delete` would refuse; see *Built-in refusals*). Review the ambiguous lines: if they are live, pass `--permissive-wildcards` or add a `wildcard_constraints` block so the two tools agree; if they are stale, pass `--delete-ambiguous` to remove them too. A wildcard with an explicit constraint is never ambiguous, unless another output, log or benchmark of the same rule leaves it unconstrained (a job made through that path can carry any value).
- **Config-conditional rules.** If a rule is `include:`-d only under specific config values, pass the matching config with `--configfile path/to/config.yaml` (repeatable). Without it, the extractor sees an empty config and config-gated rules are absent — their outputs would then be reported as orphans.
- **Function/callable paths fail loudly, never silently.** Snakemake itself rejects a rule whose `output:`, `log:` or `benchmark:` is a function or `lambda` at load time, so such a workflow never loads, and the extractor carries its own guard for the same case. `snakeprune` surfaces that as an error (exit 4) rather than guessing — it never turns an unrecognised path into a bogus orphan pattern.
- **Symlinked subdirectories are never recursed.** `--follow-symlinks` covers symlinked *files* only. Files reachable only via a symlinked subdirectory are never scanned, with or without the flag. The CLI surfaces a one-line count of skipped symlinked subdirectories at the end of the walk so this is at least visible.
- **Undeclared sidecar files are orphans.** Only paths a rule *declares* (`output`, `log`, `benchmark`) are live. Index and companion files that tools write alongside a declared output — `.bai`, `.csi`, `.tbi`, `.fai`, `.dict`, `Rplots.pdf`, `samtools sort` temporaries, `*.log` written by the tool rather than by `log:` — are reported as orphans. Declare them (`multiext`, `touch()`, an extra `output:` entry) or protect them with `--ignore "**/*.bai"` (which matches at any depth, including the top level).
- **`directory()` outputs.** Everything beneath a `directory()` output is treated as live, because Snakemake owns the whole tree. `snakeprune` cannot see stale files *inside* such a directory.
- **Do not run while the workflow is running.** In-flight jobs write temporary and partial files that match no pattern; wait for `snakemake` to finish before pruning. `snakeprune` refuses to scan (dry run included) while `.snakemake/locks` in the working directory is non-empty, but it only looks in the working directory it has worked out (see *Nested results directories*), so do not rely on that check alone.
- **Unicode forms.** Rule paths, `--ignore` globs and file names are compared in NFC form, so a name stored decomposed on disk (older macOS, some network filesystems) still matches a rule written with composed characters.
- **Module-imported rules** (`module foo: snakefile: ...; use rule * from foo`) are supported, including the `prefix:` directive. Less-common configurations (heavy `with: output:` overrides, deeply nested modules) are not specifically tested — review the orphan list before deleting.
- **No multi-config awareness.** Pipelines that legitimately produce outputs under multiple configs (e.g. running with `sample_set=A` and later `sample_set=B`) need separate scans per config; `snakeprune` only sees one config at a time.

## Runtime requirements

`snakeprune` invokes a small standalone Python script (`_extract.py`) as a subprocess to load the workflow and emit its rule outputs as JSON. The subprocess is launched with whichever `python` is on `$PATH`, so the user is expected to have activated their workflow's environment before running `snakeprune` (the same env where they would run `snakemake`).

Concretely this means:

- `snakemake` and any Python deps the workflow imports at parse time (`pandas`, `polars`, gene lists, config files) must be importable from the activated env.
- `snakeprune` itself has no `snakemake` runtime dependency, so it can be installed once via `pipx` and reused across many workflows.

If `python` is not on `$PATH`, `snakeprune` exits with code 4 and a message asking the user to activate their workflow env. If `python` is found but `snakemake` is not importable from it, `snakeprune` exits with code 4 and a more specific message pointing at the same fix.

## How it works

1. Resolve the Snakefile (direct, then `workflow/Snakefile` fallback).
2. Locate `python` on `$PATH` and run the bundled `_extract.py` script as a subprocess: `<python> .../_extract.py <pipeline_dir> [--configfile ...]`. The subprocess loads the workflow via `snakemake.api.SnakemakeApi`, walks `workflow.rules`, and emits `{"rules": [...]}` JSON on stdout.
3. For each path a rule produces (`output`, `log`, and `benchmark`) or reads (`input`), parse `{wildcard}` and `{wildcard,regex}` placeholders exactly as Snakemake does — Snakemake has already inlined rule-local and global `wildcard_constraints` into each string, and the inline annotation wins; unconstrained wildcards default to `[^/]+` (or Snakemake's `.+` under `--permissive-wildcards`). `directory()` outputs, and every input, additionally match everything beneath them. A job's wildcard values come from matching a requested file against any one of the rule's outputs, logs or benchmarks (Snakemake matches all three), and every one of them and every input is then written with those values, so each wildcard accepts, in every path of the rule, the union of its constraints over the rule's outputs, logs and benchmarks (with output `a` as `{s,\d+}` and output `b` as `{s}`, a job made for `b` with `s=abc` also writes `abc.a`). Rule paths are normalised first (`./`, `//`, trailing `/`, `seg/..`). The combined alternation regex is built once for the whole scan; patterns whose constraints use numeric backreferences are kept out of it and matched individually.
4. Walk the `results/` tree, skipping symlinks by default (symlinked directories always), any `--exclude-dir` subtree, and any path matching an `--ignore` glob.
5. For each regular file, prefix its path with the results directory's path relative to the workflow's working directory (and, separately, with the results directory's basename), then match against all compiled rule patterns. Files matching under neither prefix are orphans; an orphan that would match if unconstrained wildcards could span `/` is ambiguous and is reported on stderr.
6. With `--rule-attribution`, report each orphan with the rule whose literal path prefix it most closely resembles.
7. With `--delete` (or `--trash DIR`), prompt for confirmation, then, leaving out ambiguous orphans unless `--delete-ambiguous` is passed, either unlink each orphan one at a time or move it into `DIR/<results-dir-name>/<rel-path>` (regular files only; symlinks refused unless `--allow-symlinks`).

## Licence

MIT; see [LICENSE](LICENSE).
