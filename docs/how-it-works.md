# How it works

1. Resolve the Snakefile (direct, then `workflow/Snakefile` fallback).
2. Locate `python` on `$PATH` and run the bundled `_extract.py` script as a subprocess: `<python> .../_extract.py <pipeline_dir> [--configfile ...]`. The subprocess loads the workflow via `snakemake.api.SnakemakeApi`, walks `workflow.rules`, and emits `{"rules": [...]}` JSON on stdout.
3. For each path a rule produces (`output`, `log`, and `benchmark`) or reads (`input`), parse `{wildcard}` and `{wildcard,regex}` placeholders exactly as Snakemake does — Snakemake has already inlined rule-local and global `wildcard_constraints` into each string, and the inline annotation wins; unconstrained wildcards default to `[^/]+` (or Snakemake's `.+` under `--permissive-wildcards`). `directory()` outputs, and every input, additionally match everything beneath them. A job's wildcard values come from matching a requested file against any one of the rule's outputs, logs or benchmarks (Snakemake matches all three), and every one of them and every input is then written with those values, so each wildcard accepts, in every path of the rule, the union of its constraints over the rule's outputs, logs and benchmarks (with output `a` as `{s,\d+}` and output `b` as `{s}`, a job made for `b` with `s=abc` also writes `abc.a`). Rule paths are normalised first (`./`, `//`, trailing `/`, `seg/..`). The combined alternation regex is built once for the whole scan; patterns whose constraints use numeric backreferences are kept out of it and matched individually.
4. Walk the `results/` tree, skipping symlinks by default (symlinked directories always), any `--exclude-dir` subtree, and any path matching an `--ignore` glob.
5. For each regular file, prefix its path with the results directory's path relative to the workflow's working directory (and, separately, with the results directory's basename), then match against all compiled rule patterns. Files matching under neither prefix are orphans; an orphan that would match if unconstrained wildcards could span `/` is ambiguous and is reported on stderr.
6. With `--rule-attribution`, report each orphan with the rule whose literal path prefix it most closely resembles.
7. With `--delete` (or `--trash DIR`), prompt for confirmation, then, leaving out ambiguous orphans unless `--delete-ambiguous` is passed, either unlink each orphan one at a time or move it into `DIR/<results-dir-name>/<rel-path>` (regular files only; symlinks refused unless `--allow-symlinks`).

## Runtime requirements

`snakeprune` invokes a small standalone Python script (`_extract.py`) as a subprocess to load the workflow and emit its rule outputs as JSON. The subprocess is launched with whichever `python` is on `$PATH`, so the user is expected to have activated their workflow's environment before running `snakeprune` (the same env where they would run `snakemake`).

Concretely this means:

- `snakemake` and any Python deps the workflow imports at parse time (`pandas`, `polars`, gene lists, config files) must be importable from the activated env.
- `snakeprune` itself has no `snakemake` runtime dependency, so it can be installed once via `pipx` and reused across many workflows.

If `python` is not on `$PATH`, `snakeprune` exits with code 4 and a message asking the user to activate their workflow env. If `python` is found but `snakemake` is not importable from it, `snakeprune` exits with code 4 and a more specific message pointing at the same fix.
