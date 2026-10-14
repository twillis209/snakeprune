# snakeprune safety-review fixes — design

Date: 2026-10-14. Source: whole-repo safety review (Snakemake 9.27.0, Python 3.12).

## Confirmed data-loss bugs

1. **Brace quantifiers in wildcard constraints.** `_extract.py`'s
   `_INLINE_CONSTRAINT_RE` body `[^{}]*` cannot match `[0-9]{1,2}` or `[a-z]{3}`.
   Snakemake inlines rule-level *and* global constraints into every
   `output`/`log`/`benchmark` path string before snakeprune sees it, so a global
   `wildcard_constraints: chrom="[0-9]{1,2}"` (ubiquitous in genomics) leaves the
   raw `{chrom,[0-9]{1,2}}` in the pattern; `patterns.py`'s `_WILDCARD_RE` then
   also fails to match it, the whole path is `re.escape`d to a literal that
   matches nothing, and every file of that rule is reported as an orphan.
2. **`directory()` outputs.** Pattern `^results/(?P<s>[^/]+)/outdir$` does not
   match files inside the directory (`outdir/x.txt`, `outdir/.snakemake_timestamp`);
   all are orphaned.
3. **Default wildcard regex.** `[^/]+` is stricter than Snakemake's `.+`; README
   falsely claims parity. A file produced with a `/` inside a wildcard value is
   orphaned.
4. **Inline-constraint cross-contamination.** `_extract.py` merges inline
   constraints from all produced paths of a rule into one dict (last wins), so a
   `log:` constraint can rewrite the `output:` regex.
5. **Absolute output paths** (e.g. `config["outdir"]` absolute) never match the
   `<basename>/<rel>` target, so everything that rule writes is orphaned.
6. (minor) Constraint precedence: Snakemake is inline > rule-local > global;
   snakeprune applied rule-local over inline.

## Execution-safety gaps (by reading)

7. `delete_orphans` validation is all-or-nothing but execution is not; a
   mid-batch `OSError` either prints "Refusing to delete" after files are gone
   (`PermissionError`) or escapes as a traceback (`FileNotFoundError` etc.).
8. No guard against `--trash` inside `results_dir`.
9. `walker.find_orphans` is an unused second entry point with no refusals.

## Hygiene (from /code-review of the last commit)

10. Callable `log:`/`benchmark:` not guarded by the extractor itself.
11. `getattr(rule, "benchmark", None)` vs direct `rule.log`.
12. Duplicate produced paths not deduplicated.
13. "output pattern(s)" wording now covers log/benchmark.
14. Named log entries and `repeat()` benchmarks untested.
15. README: sidecar files, `directory()` behaviour, concurrent-run warning.

## Decisions (confirmed with the user)

- **D1 — one parser.** The extractor stops parsing braces entirely. It emits
  each produced path as the raw annotated string `str(o)` returns, plus a
  `directories` list naming the `directory()`-flagged subset, plus
  `constraints` (rule-local merged over global) kept only as a fallback. The
  single wildcard parser lives in `patterns.py`, modelled on Snakemake's
  `WILDCARD_REGEX` (constraint body `(?:[^{}]+|\{\d+(?:,\d+)?\})*`).
  Precedence: inline > `constraints` dict > default. This resolves 1, 4, 6.
- **D2 — loud failure.** Any `{` or `}` surviving in a literal segment after
  wildcard substitution raises `PatternError`; the CLI maps it to exit 4.
- **D3 — directory outputs** get `(?:/.*)?` appended before `$`, so files
  inside are live.
- **D4 — keep `[^/]+` as default** (it is what makes the tool useful for its
  motivating case); fix the README claim; add `--permissive-wildcards` to opt
  into Snakemake's `.+`.
- **D5 — absolute paths.** The extractor relativises absolute paths that sit
  under the workdir. `find_rule_patterns` skips any remaining absolute pattern
  and reports it through an `on_warning` callback; the CLI prints that warning
  regardless of `--quiet`.
- **D6 — partial deletion is reported honestly.** The delete/trash loop wraps
  each operation; on `OSError` it raises `DeletionFailed(completed, total,
  path)`. The CLI prints "Deleted N of M … failed at X" and exits 5.
- **D7 — refuse `--trash` inside `results_dir`** (exit 3).
- **D8 — delete `walker.find_orphans`** and its tests.

## Exit codes after this work

| code | meaning |
|---|---|
| 0 | success / user aborted at prompt |
| 2 | Snakefile not found |
| 3 | refused (guard tripped) before any destructive action |
| 4 | extractor or pattern failure (workflow unloadable, unparseable pattern) |
| 5 | deletion started but did not complete; message states how many were removed |
