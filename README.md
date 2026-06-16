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
```

`path/to/pipeline` is the directory containing the Snakefile (e.g. the `smk/` directory for projects following the recommended `workflow/Snakefile` layout, or whatever directory holds your `Snakefile`). `snakeprune` resolves the entry point as `<pipeline>/Snakefile` first, then falls back to `<pipeline>/workflow/Snakefile`.

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
