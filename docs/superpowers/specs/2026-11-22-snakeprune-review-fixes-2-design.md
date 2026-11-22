# snakeprune review fixes, round 2 — design

Date: 2026-11-22. Source: whole-repo code review (Snakemake 9.27.2, Python 3.12).
Baseline: `python -m pytest -q` → 144 passed in the project `.venv`.

## Intent

Ten findings, six of which can cause live files to be listed as orphans (and
deleted under `--delete`). Fix all ten. The governing rule for every fix:
**when in doubt, the file stays** — a fix may make the tool refuse or keep more,
never delete more.

## Non-goals

- No new features, no flag renames, no change to exit-code meanings.
- `--follow-symlinks` does not start descending into symlinked directories.
- The trash layout (`<trash>/<results_dir.name>/<rel>`) does not change.
- `--ignore` does not move to gitignore semantics (`*` keeps crossing `/`).

## Approaches considered

1. **Ten narrow fixes, one failing test each, landed sequentially** (chosen).
   Each finding is independent in behaviour, though several share a file.
2. Rework matching around absolute paths (resolve every rule path against the
   workdir, match files by absolute path). Would subsume #2 and #4 but rewrites
   the matching core and the trash/guard logic for little extra safety.
3. Guards only (refuse/warn on suspicious input rather than fix). Leaves the
   tool unusable on ordinary workflows (#2, #7).

## Decisions

Findings are numbered as in the review.

- **F1 — `--exclude-dir` must exist (confirmed by the user; reverses the
  2026-06-23 "non-existent dir → no error" decision).** Before the walk, each
  value is resolved (absolute as-is, relative against `results_dir`) and must be
  an existing, non-symlink directory strictly inside `results_dir`. Containment
  is checked in both lexical (`abspath`) and `realpath` forms; a value given by
  real path while `results_dir` is a symlinked path is re-anchored onto the
  form the walker builds (`os.path.join(results_dir, rel)`) so it actually
  prunes. Anything else → message naming the offending value and the resolved
  path, exit 3, nothing scanned. No cwd-relative fallback.
- **F2 — normalise rule paths.** Confirmed empirically: the extractor emits
  `./results/a/{s}.txt`, `results//b/{s}.txt`, `results/sub/../d/{s}.txt`
  verbatim; `directory("results/out/{s}/")` already arrives without the
  trailing slash. `find_rule_patterns` normalises each path before building the
  regex, treating `{…}` wildcards as opaque tokens: drop `.` segments, collapse
  repeated `/`, drop a trailing `/`, and collapse `seg/..` only when `seg` is a
  literal segment. A path that *leads* with `..` is resolved against the
  workdir to an absolute path and then takes the existing absolute-path route
  (rewritten if under the results dir, else dropped with a warning). A `..`
  that follows a wildcard-bearing segment cannot be resolved: it raises
  `PatternError` (exit 4) rather than warn, because a warning would still let
  that rule's files be listed and deleted (user decision 2026-11-22: always
  default in favour of safety). `directories` membership is decided before
  normalising.
- **F3 — `**/` matches zero directories.** Purely additive: a glob matches if it
  matches today, or if it matches with any `**/` occurrence removed (leading
  `**/x` → `x`, inner `a/**/b` → `a/b`). Nothing ignored today stops being
  ignored.
- **F4 — workdir-relative matching.** One shared *match prefix* replaces the
  three uses of `results_dir.name + "/"` (match target, basename guard,
  absolute-path rewrite). `patterns.py` gains the workdir rule the extractor
  already uses (Snakefile under `workflow/` → its parent, else the Snakefile's
  directory). If `results_dir` (abspath or realpath) lies under the workdir
  (abspath or realpath), the prefix is its relative path + `/`; if it *is* the
  workdir, the prefix is empty; otherwise fall back to the basename, as today.
  `load_rule_specs`' signature and the stub-script tests are untouched. The
  guard's message says "No rule writes under `<prefix>`".
- **F5 — numeric backreferences.** `combine_rule_patterns` excludes any pattern
  whose source contains a backslash-digit or `(?(` (deliberately
  over-inclusive; a false positive costs only speed). Excluded patterns are
  matched individually after the combined regex misses. Combining happens
  inside the CLI's exit-4 handler, and a `re.error` while combining is raised
  as `PatternError`.
- **F6 — trash ancestors.** Pre-validation also walks each target's ancestors
  between `trash_dir` and the target; an existing non-directory there raises
  `FileExistsError` (exit 3) before anything moves. Two orphans in one batch
  where one's target is an ancestor of the other's cannot occur (both are files
  under one tree).
- **F7 — extractor stdout.** At start-up `_extract.py` duplicates fd 1 to a
  saved descriptor and `dup2`s fd 2 over fd 1 for the life of the process (no
  restore, so late-flushing buffers and child processes cannot reach the
  pipe); the JSON is written to the saved descriptor. `run_extractor`'s
  "unparseable output" error stops claiming it is a snakeprune bug.
- **F8 — symlinked directories with `--follow-symlinks`.** The
  `skipped_symlinked_dirs` counter is incremented in both modes; the flag's
  help text and the README say it covers symlinked *files* only.
- **F9 — escaped `\(` in literal prefixes.** `extract_literal_prefix` scans for
  the first *unescaped* `(`.
- **F10 — attribution cost.** Prefixes are computed once per scan.
  `attribute_orphan_to_rule(target, patterns)` keeps working; it accepts an
  optional precomputed `(name, prefix)` list which the CLI passes.

## Verification

Each fix lands test-first; the failing test is the proof the finding is real.
Only F5, F8, F9 (and now F2, F7) have been reproduced. If a test for one of the
others cannot be made to fail, that fix is dropped and reported.

## Order

Sequential (shared files): F9 → F10 → F2 → F4 → F5 → F7 → F1 → F3 → F8 → F6,
then README (exclude-dir must exist, `**/`, `--follow-symlinks` wording, nested
results dirs, `print()` now fine) and `docs/test-coverage.md` counts.

## Left to the builder

Helper names, exact message wording, and test placement within the existing
`tests/test_*.py` files.
