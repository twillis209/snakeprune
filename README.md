# snakeprune

Find orphan files in a Snakemake project's `results/` directory — files that no rule in the current pipeline can produce.

## Motivation

As a Snakemake pipeline evolves, new wildcards get added to output paths (e.g., adding `{pc_orth}` between two existing path segments). Pre-existing files on disk are stranded: they no longer match any rule's output pattern, but Snakemake doesn't know to clean them up. Over the lifetime of an active project this can accumulate to tens of thousands of obsolete files, consuming space and tripping HPC file-count limits.

`snakeprune` ingests the pipeline definitions via Snakemake's Python API, builds a regex pattern per rule output (using each rule's `wildcard_constraints`), walks the project's `results/` tree, and reports files that match no pattern.

## Status

Early development. Design at `docs/superpowers/specs/2026-06-16-snakeprune-design.md`.
