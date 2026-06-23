# snakeprune

Find 'orphan' files in a `snakemake` project's `results/` directory and delete them.

## Motivation

When developing a `snakemake` pipeline, I usually write and run rules iteratively, updating the output path with more and more wildcards such that I end up with many versions of a rule's output files, e.g.
* `results/{sample}/{replicate}/output.txt` becomes
* `results/{sample}/{replicate}/{normalisation}/output.txt` becomes
* `results/{sample}/{replicate}/{normalisation}/{count_threshold}/output.txt`

...and so on. If I settle on the third of those output paths, the first two files will be left in the `results` directory tree. When working with very large numbers of files, say, one per human gene, multiple iterations clutter up your filesystem over time. To my knowledge `snakemake` provides no way of cleaning these files up.

`snakeprune` ingests the workflow by invoking a small extractor script via subprocess in the workflow's own Python environment, builds a regex pattern per rule output, walks the project's `results/` tree, and reports files that match no pattern. `snakeprune` can delete these files or move them to a designated trash directory whilst preserving the same output path from `results` onwards.

## Install

`snakeprune` itself is a small pure-Python CLI. The recommended install is via [`pipx`](https://pipx.pypa.io/) so it lives in its own isolated env and works against any workflow:

```bash
pipx install snakeprune
```

Or, to install from a clone:

```bash
git clone https://github.com/twillis209/snakeprune.git
cd snakeprune
pip install -e .
```

`snakeprune` calls into the workflow's own Python environment via subprocess, so `snakemake` must be importable in *that* environment — but it does not need to live in the same env as `snakeprune`.

## Usage

```bash
# Dry-run: list orphans, take no action
snakeprune scan path/to/pipeline path/to/results

# With rule attribution (best-guess rule per orphan)
snakeprune scan path/to/pipeline path/to/results --rule-attribution

# Skip intentional manual files (repeatable; supports prefix/** for directories)
snakeprune scan path/to/pipeline path/to/results --ignore "notes/**" --ignore "*.log"

# Skip an entire subtree from the walk (faster than --ignore for big dirs)
snakeprune scan path/to/pipeline path/to/results --exclude-dir old_results_to_keep

# Actually delete (refuses symlinks unless --allow-symlinks)
snakeprune scan path/to/pipeline path/to/results --delete

# Non-interactive delete (for scripts)
snakeprune scan path/to/pipeline path/to/results --delete --yes

# Reversible delete: move orphans into a trash directory instead of unlinking
snakeprune scan path/to/pipeline path/to/results --trash path/to/trash

# Pass config to the workflow loader so config-gated rules are visible
snakeprune scan path/to/pipeline path/to/results --configfile path/to/config.yaml
```

`path/to/pipeline` is the directory containing the Snakefile (e.g. the `smk/` directory for projects following the recommended `workflow/Snakefile` layout, or whatever directory holds your `Snakefile`). `snakeprune` resolves the entry point as `<pipeline>/Snakefile` first, then falls back to `<pipeline>/workflow/Snakefile`.

## Safety and limitations

`snakeprune` is destructive at the user's request, so the CLI tries hard to refuse rather than do the wrong thing.

### Built-in refusals

- **Empty rule list.** If the workflow loads but produces zero output patterns (e.g. all rules are gated behind config that wasn't passed), `snakeprune` refuses to scan rather than report every file as an orphan. Override with `--allow-empty-rules`.
- **Results-dir / rule-prefix mismatch.** If you point at `path/to/foo/` but no rule writes under `foo/`, every file would look like an orphan. `snakeprune` refuses and surfaces the prefixes the rules actually use. Override with `--allow-basename-mismatch`.
- **High orphan rate.** When more than `--orphan-rate-threshold` (default 0.5) of scanned files would be orphans, `snakeprune` prints a loud warning and — under `--delete` / `--trash` — refuses unless `--allow-high-orphan-rate` is also passed. Pass `--orphan-rate-threshold 1.0` to disable the check entirely.
- **Naughty target directories.** Targeting a conventional Snakemake input/config directory (`resources`, `config`, `profile`, `workflow`, `.snakemake`) prints a warning, and — under `--delete` / `--trash` — refuses unless `--allow-naughty-dir` is passed. The check fires before the workflow is even loaded, and especially guards against pointing at `resources/` (downloaded reference data). Extend the list for a run with `--naughty-dir NAME` (repeatable).

### Deletion flow

- Default behaviour without `--delete` or `--trash` is dry-run: list orphans and take no action.
- `--delete` shows a summary (count + total bytes) and prompts `[y/N]` before unlinking.
- `--yes` skips the prompt; required when stdin is not a TTY (e.g. in scripts).
- `--trash DIR` moves each orphan to `DIR/<results-dir-name>/<rel-path>` instead of unlinking, so deletions are reversible. Passing `--trash` implies deletion — you do not also need `--delete`.

### Recommended first-use workflow

1. Run a plain `snakeprune scan <pipeline> <results>` and eyeball the orphan list.
2. If the list looks right, re-run with `--trash some/staging/dir` to move (not unlink) the orphans.
3. After a few days of confidence that nothing's missing, delete the trash directory.

### Limitations

- **`--exclude-dir` vs `--ignore`:** `--exclude-dir DIR` prunes a whole subtree from the walk (the directory is never descended into), which is faster than `--ignore "DIR/**"` for large directories you want to protect. `--ignore` remains for file-level glob filtering. Relative `--exclude-dir` paths resolve against the results directory.
- **Config-conditional rules.** If a rule is `include:`-d only under specific config values, pass the matching config with `--configfile path/to/config.yaml` (repeatable). Without it, the extractor sees an empty config and config-gated rules are absent — their outputs would then be reported as orphans.
- **Function/callable outputs fail loudly, never silently.** Snakemake itself rejects a rule whose `output:` is a function or `lambda` (`Only input files can be specified as functions`), so such a workflow never loads. `snakeprune` surfaces that as an error (exit 4) rather than guessing — it never turns an unrecognised output into a bogus orphan pattern.
- **Symlinked subdirectories are not recursed.** Files reachable only via a symlinked subdirectory are never scanned. The CLI surfaces a one-line count of skipped symlinked subdirectories at the end of the walk so this is at least visible.
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
3. For each rule's outputs, substitute `{wildcard}` placeholders with their effective regex bodies (rule-local `wildcard_constraints` override workflow-global; inline `{name,regex}` annotations are honoured; missing constraints default to `[^/]+`). The combined alternation regex is built once for the whole scan.
4. Walk the `results/` tree, skipping symlinks by default and any path matching an `--ignore` glob.
5. For each regular file, match the path against all compiled rule patterns. Files matching none are orphans.
6. With `--rule-attribution`, report each orphan with the rule whose literal path prefix it most closely resembles.
7. With `--delete` (or `--trash DIR`), prompt for confirmation, then either unlink each orphan one at a time or move it into `DIR/<results-dir-name>/<rel-path>` (regular files only; symlinks refused unless `--allow-symlinks`).
