# snakeprune

Find orphan files in a Snakemake project's `results/` directory — files that no rule in the current pipeline can produce.

## Motivation

As a Snakemake pipeline evolves, new wildcards get added to output paths (e.g., adding `{pc_orth}` between two existing path segments). Pre-existing files on disk are stranded: they no longer match any rule's output pattern, but Snakemake doesn't know to clean them up. Over the lifetime of an active project this can accumulate to tens of thousands of obsolete files, consuming space and tripping HPC file-count limits.

`snakeprune` ingests the pipeline definitions via Snakemake's Python API, builds a regex pattern per rule output (using each rule's `wildcard_constraints`), walks the project's `results/` tree, and reports files that match no pattern.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Dry-run: list orphans, take no action
snakeprune scan path/to/pipeline path/to/results

# With rule attribution (best-guess rule per orphan)
snakeprune scan path/to/pipeline path/to/results --rule-attribution

# Skip intentional manual files (repeatable; supports prefix/** for directories)
snakeprune scan path/to/pipeline path/to/results --ignore "notes/**" --ignore "*.log"

# Actually delete (refuses symlinks unless --allow-symlinks)
snakeprune scan path/to/pipeline path/to/results --delete

# Non-interactive delete (for scripts)
snakeprune scan path/to/pipeline path/to/results --delete --yes

# Reversible delete: move orphans into a trash directory instead of unlinking
snakeprune scan path/to/pipeline path/to/results --trash path/to/trash
```

`path/to/pipeline` is the directory containing the Snakefile (e.g. the `smk/` directory for projects following the recommended `workflow/Snakefile` layout, or whatever directory holds your `Snakefile`). `snakeprune` resolves the entry point as `<pipeline>/Snakefile` first, then falls back to `<pipeline>/workflow/Snakefile`.

## Safety and limitations

`snakeprune` is destructive at the user's request, so the CLI tries hard to refuse rather than do the wrong thing.

### Built-in refusals

- **Empty rule list.** If the workflow loads but produces zero output patterns (e.g. all rules are gated behind config that wasn't passed), `snakeprune` refuses to scan rather than report every file as an orphan. Override with `--allow-empty-rules`.
- **Results-dir / rule-prefix mismatch.** If you point at `path/to/foo/` but no rule writes under `foo/`, every file would look like an orphan. `snakeprune` refuses and surfaces the prefixes the rules actually use. Override with `--allow-basename-mismatch`.
- **High orphan rate.** When more than `--orphan-rate-threshold` (default 0.5) of scanned files would be orphans, `snakeprune` prints a loud warning and — under `--delete` / `--trash` — refuses unless `--allow-high-orphan-rate` is also passed. Pass `--orphan-rate-threshold 1.0` to disable the check entirely.

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

- **Config-conditional rules.** If a rule is only `include:`-d under specific config values, running `snakeprune` with a different config will not see that rule, and its outputs will be reported as orphans. Always run `snakeprune` in the same env / with the same config used to produce the files.
- **Symlinked subdirectories are not recursed.** Files reachable only via a symlinked subdirectory are never scanned. The CLI surfaces a one-line count of skipped symlinked subdirectories at the end of the walk so this is at least visible.
- **Module-imported rules** (`module foo: snakefile: ...; use rule * from foo`) are supported, including the `prefix:` directive. Less-common configurations (heavy `with: output:` overrides, deeply nested modules) are not specifically tested — review the orphan list before deleting.
- **No multi-config awareness.** Pipelines that legitimately produce outputs under multiple configs (e.g. running with `sample_set=A` and later `sample_set=B`) need separate scans per config; `snakeprune` only sees one config at a time.

## Runtime requirements

Because `snakeprune` uses Snakemake's Python API to load the workflow in-process, **the workflow must be loadable in the environment where `snakeprune` is invoked**. This means:

- All Python dependencies the workflow imports at top level (e.g. `pandas`, `polars`) must be installed.
- Any files the workflow reads at parse time (gene lists, config files, resource bundles) must be accessible from the directory you point `snakeprune` at.

In practice this usually means running `snakeprune` in the same environment / on the same host where you normally run `snakemake`. For HPC pipelines this is your usual login or compute node, not your laptop. A separate process / subprocess-based approach that avoids in-process workflow loading is a possible future direction.

## How it works

1. Resolve the Snakefile (direct, then `workflow/Snakefile` fallback).
2. Load the workflow via `snakemake.api.SnakemakeApi`, enumerate `workflow.rules`.
3. For each rule's `output` patterns, substitute `{wildcard}` placeholders with their effective regex bodies (rule-local `wildcard_constraints` override workflow-global; inline `{name,regex}` annotations are honoured; missing constraints default to `[^/]+`).
4. Walk the `results/` tree, skipping symlinks by default and any path matching an `--ignore` glob.
5. For each regular file, match the path against all compiled rule patterns. Files matching none are orphans.
6. With `--rule-attribution`, report each orphan with the rule whose literal path prefix it most closely resembles.
7. With `--delete`, unlink each orphan one at a time (regular files only; symlinks refused unless `--allow-symlinks`).

## Status

MVP. Design at `docs/superpowers/specs/2026-06-16-snakeprune-design.md`; implementation plan at `docs/superpowers/plans/2026-06-16-snakeprune-mvp.md`.
