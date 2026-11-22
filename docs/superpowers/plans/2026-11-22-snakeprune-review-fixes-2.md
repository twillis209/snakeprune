# snakeprune Review Fixes (Round 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ten findings of the 2026-11-22 code review so snakeprune never lists a live file as an orphan because of them.

**Architecture:** Ten narrow, test-first fixes in the existing modules (`patterns.py`, `walker.py`, `delete.py`, `cli.py`, `_extract.py`), landed sequentially because several share a file. No new modules.

**Tech Stack:** Python 3.12, typer, pytest, Snakemake 9.27.2 (project `.venv`).

**Spec:** `docs/superpowers/specs/2026-11-22-snakeprune-review-fixes-2-design.md`

## Global Constraints

- **When in doubt, the file stays.** A fix may make the tool refuse or keep more, never delete more.
- No new features, no flag renames, no change to exit-code meanings (0 ok/aborted, 2 no Snakefile, 3 refused, 4 extractor/pattern failure, 5 partial deletion).
- `--follow-symlinks` does not descend into symlinked directories. Trash layout stays `<trash>/<results_dir.name>/<rel>`. `--ignore`'s `*` keeps crossing `/`.
- `_extract.py` must not import from `snakeprune`. `load_rule_specs`' signature is unchanged.
- Run tests as `PATH="$PWD/.venv/bin:$PATH" python -m pytest -q` (the extractor runs whatever `python` is first on PATH). Baseline: 144 passed. The full suite takes ~90 s; run the single new test while iterating and the full suite before each commit.
- Each task: failing test first. **If the new test passes before any code change, stop and report it** — the finding is not real and the fix is dropped.
- One commit per task, on branch `fix/review-findings-2`, then `git push`. End every commit message with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- Tests go in the existing `tests/test_*.py` file for the module, using the `make_pipeline` / `make_results` fixtures and `CliRunner` as the neighbouring tests do. Existing tests that encode behaviour a task deliberately changes are updated in that task and named in the commit message.

## Review Focus

Inputs the spec implies that are most likely to bite; each is pinned by a test in the task named.

1. Results dir reached through a symlink while the workdir is a real path (or vice versa): files still match. → Task 4.
2. Results dir *is* the workdir (scanning the project root): empty prefix, nothing crashes, `results/x.txt` matches. → Task 4.
3. `--exclude-dir` given by real path while `results_dir` is a symlinked path: the subtree really is pruned. → Task 7.
4. A Snakefile that writes to stdout from a child process (`os.system("echo hi")`), not just `print()`. → Task 6.
5. A rule path with `..` after a wildcard segment (`results/{s}/../x.txt`): a warning, not a crash and not a silently wrong pattern. → Task 3.

---

### Task 1: Escape-aware literal prefix (F9)

**Files:** Modify `src/snakeprune/patterns.py` (`extract_literal_prefix`); Test `tests/test_patterns.py`.

- [ ] **Step 1: Failing test**

```python
def test_extract_literal_prefix_ignores_escaped_paren():
    regex = wildcard_pattern_to_regex("results/run(1)/{s}.txt", constraints={})
    assert extract_literal_prefix(re.compile(regex)) == "results/run(1)/"

def test_extract_literal_prefix_escaped_paren_without_wildcard():
    regex = wildcard_pattern_to_regex("results/run(1)/x.txt", constraints={})
    assert extract_literal_prefix(re.compile(regex)) == "results/run(1)/x.txt"
```

- [ ] **Step 2:** Run them; expect FAIL (prefix ends `results/run\`).
- [ ] **Step 3:** Make `extract_literal_prefix` cut at the first `(` not preceded by an odd number of backslashes (scan left to right, skipping the character after each backslash). Signature unchanged.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(patterns): literal prefix no longer cut at an escaped paren`.

### Task 2: Compute attribution prefixes once (F10)

**Files:** Modify `src/snakeprune/patterns.py`, `src/snakeprune/walker.py`, `src/snakeprune/cli.py`; Test `tests/test_walker.py` or wherever `attribute_orphan_to_rule` is tested.

**Interfaces:**
- Produces: `patterns.rule_literal_prefixes(patterns: list[tuple[str, re.Pattern]]) -> list[tuple[str, str]]` (rule name, literal prefix, same order).
- Produces: `walker.attribute_orphan_to_rule(target: str, patterns: list[tuple[str, re.Pattern]], prefixes: list[tuple[str, str]] | None = None) -> str | None`. When `prefixes` is given, `patterns` is not consulted.

- [ ] **Step 1: Failing test**

```python
def test_attribute_orphan_uses_precomputed_prefixes(monkeypatch):
    import snakeprune.walker as walker
    pats = [("a", re.compile(r"^results/a/(?P<s>[^/]+)\.txt$")),
            ("b", re.compile(r"^results/b/(?P<s>[^/]+)\.txt$"))]
    prefixes = rule_literal_prefixes(pats)
    assert prefixes == [("a", "results/a/"), ("b", "results/b/")]
    def boom(_):
        raise AssertionError("prefix recomputed per orphan")
    monkeypatch.setattr(walker, "extract_literal_prefix", boom)
    assert walker.attribute_orphan_to_rule("results/b/old.csv", pats, prefixes) == "b"
```

- [ ] **Step 2:** Run; expect FAIL (`rule_literal_prefixes` undefined).
- [ ] **Step 3:** Implement both; in `cli.scan` compute `rule_literal_prefixes(patterns)` once (reuse it for the basename guard's `rule_prefixes`) and pass it to `attribute_orphan_to_rule`.
- [ ] **Step 4:** Full suite passes (existing two-argument calls still work).
- [ ] **Step 5:** Commit `perf(walker): compute rule literal prefixes once per scan`.

### Task 3: Normalise rule paths (F2)

**Files:** Modify `src/snakeprune/patterns.py`; Test `tests/test_patterns.py`, `tests/test_cli.py`.

**Interfaces:**
- Produces: `patterns.normalise_rule_path(path: str) -> tuple[str, bool]` — the normalised path and whether an unresolvable `..` remains.

Rules (wildcards matched by `_WILDCARD_RE` are opaque tokens, never altered, and a `/` inside a constraint is not a separator): drop `.` segments; collapse repeated `/`; drop a trailing `/`; preserve a single leading `/`; collapse `seg/..` only when `seg` contains no wildcard and is not itself `..`; any other `..` stays verbatim and the flag is `True`.

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("raw,expected,flag", [
    ("./results/a/{s}.txt", "results/a/{s}.txt", False),
    ("results//b/{s}.txt", "results/b/{s}.txt", False),
    ("results/sub/../d/{s}.txt", "results/d/{s}.txt", False),
    ("results/out/{s}/", "results/out/{s}", False),
    ("/abs/./x/{s}.txt", "/abs/x/{s}.txt", False),
    ("results/{s,a/./b|c//d}.txt", "results/{s,a/./b|c//d}.txt", False),
    ("results/{s}/../x.txt", "results/{s}/../x.txt", True),
    ("../up/{s}.txt", "../up/{s}.txt", True),
    ("results/{s}.txt", "results/{s}.txt", False),
])
def test_normalise_rule_path(raw, expected, flag):
    assert normalise_rule_path(raw) == (expected, flag)
```

CLI test `test_cli_scan_unnormalised_rule_paths_are_live`: Snakefile with rules writing `./results/a/{s}.txt`, `results//b/{s}.txt`, `results/sub/../d/{s}.txt`; results `a/1.txt`, `b/1.txt`, `d/1.txt`, `stale.csv`; assert exit 0, `stale.csv` in stdout, none of the three live files in stdout.

Review Focus 5 — `test_find_rule_patterns_warns_on_dotdot_after_wildcard`: rule output `results/{s}/../x.txt`; call `find_rule_patterns(..., on_warning=msgs.append)`; assert it returns without raising and exactly one message contains both the rule name and `..`.

- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement `normalise_rule_path`; in `find_rule_patterns`, for each output: decide `is_directory` on the raw string, then normalise, warn if flagged (`WARNING: rule `<name>` path `<path>` contains `..` that snakeprune cannot resolve; files it writes may be reported as orphans.`), then the existing absolute-path handling and regex build.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(patterns): normalise ./, // and literal .. in rule paths`.

### Task 4: Workdir-relative matching (F4)

**Files:** Modify `src/snakeprune/patterns.py`, `src/snakeprune/cli.py`; Test `tests/test_patterns.py`, `tests/test_cli.py`.

**Interfaces:**
- Produces: `patterns.resolve_workdir(pipeline_dir: Path) -> Path` — parent of the Snakefile's directory if that directory is named `workflow`, else the Snakefile's directory (same rule as `_extract._resolve_workdir`; raises `SnakefileNotFound`).
- Produces: `patterns.results_match_prefix(pipeline_dir: Path, results_dir: Path) -> str` — tries every pairing of (`abspath`, `realpath`) of `results_dir` against (`abspath`, `realpath`) of the workdir: strictly under → POSIX relative path + `/`; equal → `""`; no pairing → `results_dir.name + "/"`.
- Changes: `find_rule_patterns(..., results_dir: Path | None = None, match_prefix: str | None = None)`; `match_prefix=None` means `results_dir.name + "/"` (today's behaviour, keeps existing callers). `_rewrite_under_results_dir` rewrites to `match_prefix + rel`.

- [ ] **Step 1: Failing tests**

`test_results_match_prefix` (unit, `tmp_path`): pipeline `p/` with `Snakefile`; `p/results` → `"results/"`; `p/results/qc` → `"results/qc/"`; `p` itself → `""`; `tmp_path/elsewhere/out` → `"out/"`; pipeline `q/workflow/Snakefile` with `q/results` → `"results/"`.

`test_cli_scan_nested_results_dir_uses_workdir_relative_path`: rules write `results/qc/{s}.txt` and `qc/{s}.html`; create `pipeline/results/qc/1.txt` and `pipeline/results/qc/stale.csv` inside the pipeline dir; scan `pipeline/results/qc`; assert exit 0, `1.txt` not in stdout, `stale.csv` in stdout.

Review Focus 1 — `test_cli_scan_results_dir_via_symlink_matches`: as above but scan through `tmp_path/link -> pipeline/results/qc`; `1.txt` not in stdout.

Review Focus 2 — `test_cli_scan_results_dir_equal_to_workdir`: rule writes `results/{s}.txt`; files `pipeline/results/1.txt`, `pipeline/results/stale.csv`; scan `pipeline pipeline --ignore "Snakefile" --ignore ".snakemake/**"`; exit 0, `stale.csv` listed, `1.txt` not.

`test_cli_basename_guard_uses_match_prefix`: rules write only `other/{s}.txt`; scan `pipeline/results/qc` (exists, one file); exit 3 and stderr contains ``No rule writes under `results/qc/` ``.

- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement the two functions. In `cli.scan`, compute `match_prefix = results_match_prefix(pipeline_dir, results_dir)` inside the existing `try` (so `SnakefileNotFound` → exit 2), pass it to `find_rule_patterns`, and use it for the basename guard and the match target. Leave `results_dir_name=results_dir.name` for the trash call. When the prefix is `""`, the guard passes trivially.
- [ ] **Step 4:** Full suite passes (fixtures keep results outside the pipeline dir, so they exercise the basename fallback).
- [ ] **Step 5:** Commit `fix: match scanned files by path relative to the workflow workdir`.

### Task 5: Numeric backreferences in the combined pattern (F5)

**Files:** Modify `src/snakeprune/patterns.py`, `src/snakeprune/cli.py`; Test `tests/test_patterns.py`, `tests/test_cli.py`.

**Interfaces:**
- Changes: `combine_rule_patterns(patterns) -> CombinedPattern | None`. `CombinedPattern.match(target: str) -> bool` is true if the combined alternation matches or any individually-kept pattern matches. Update existing tests that rely on the return being an `re.Pattern` only as far as needed.

- [ ] **Step 1: Failing tests**

```python
def test_combine_rule_patterns_keeps_numeric_backrefs_working():
    pats = [("a", re.compile(r"^results/(?P<s>(a|b)\2)\.txt$")),
            ("b", re.compile(r"^results/x/(?P<s>[^/]+)\.txt$"))]
    combined = combine_rule_patterns(pats)
    assert combined.match("results/aa.txt")
    assert not combined.match("results/ab.txt")
    assert combined.match("results/x/1.txt")

def test_combine_rule_patterns_numeric_backrefs_do_not_cross_rules():
    pats = [("a", re.compile(r"^results/a/(?P<s>(x|y)\2)\.txt$")),
            ("b", re.compile(r"^results/b/(?P<s>(p|q)\2)\.txt$"))]
    combined = combine_rule_patterns(pats)
    assert combined.match("results/b/pp.txt")
    assert not combined.match("results/b/pq.txt")
```

CLI test `test_cli_scan_constraint_with_numeric_backref`: rule output `results/{s,(a|b)\2}.txt` (write the Snakefile with a raw string); files `aa.txt`, `stale.csv`; exit 0, `aa.txt` not listed, `stale.csv` listed.

- [ ] **Step 2:** Run; expect FAIL with `re.error: invalid group reference`.
- [ ] **Step 3:** In `combine_rule_patterns`, set aside any pattern whose source matches `r"\\\d|\(\?\("` (over-inclusive on purpose) and keep it compiled as-is; combine the rest as today; wrap the final `re.compile` so `re.error` becomes `PatternError`. In `cli.scan`, move the `combine_rule_patterns` call inside the `try` that maps `PatternError` to exit 4.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(patterns): match numeric-backreference constraints outside the combined regex`.

### Task 6: Keep workflow stdout out of the extractor's JSON (F7)

**Files:** Modify `src/snakeprune/_extract.py`, `src/snakeprune/patterns.py` (error text); Test `tests/test_patterns.py`, `tests/test_cli.py`.

- [ ] **Step 1: Failing tests**

`test_run_extractor_tolerates_print_in_snakefile`: Snakefile begins `print("Loaded 12 samples")` then a rule writing `results/{s}.txt`; `run_extractor(pipeline)` returns one spec named for the rule.

Review Focus 4 — `test_run_extractor_tolerates_child_process_stdout`: Snakefile begins `import os, sys`, `os.system("echo from-child")`, `sys.stdout.write("x" * 100000)`; same assertion.

`test_cli_scan_with_print_in_snakefile`: as the first, via the CLI; exit 0 and `Loaded 12 samples` not in `result.stdout`.

- [ ] **Step 2:** Run; expect FAIL with `ExtractorError` "unparseable output".
- [ ] **Step 3:** At the top of `_extract.main`, before argument parsing: flush `sys.stdout`, `saved = os.dup(1)`, `os.dup2(2, 1)`; do not restore. Write the JSON with `os.fdopen(saved, "w")` and close it. In `run_extractor`, reword the `JSONDecodeError` message to `Extractor produced unparseable output on stdout. stderr was:` (drop "This is a bug; please report").
- [ ] **Step 4:** Full suite passes (update any test asserting the old wording).
- [ ] **Step 5:** Commit `fix(extract): workflow stdout no longer corrupts the extractor payload`.

### Task 7: `--exclude-dir` must name an existing directory inside the results dir (F1)

**Files:** Modify `src/snakeprune/cli.py`; Test `tests/test_cli.py`.

**Interfaces:**
- Produces: `cli._resolve_exclude_dirs(results_dir: Path, values: Sequence[str]) -> list[str]`, raising `cli.ExcludeDirError(ValueError)`. Returned strings are in the form the walker compares against: `os.path.abspath(os.path.join(results_dir, rel))`.

Rules per value: absolute as-is, relative joined onto `results_dir`. It must exist, be a directory, not be a symlink, and lie strictly inside `results_dir`, where "inside" is true if either the `abspath` forms or the `realpath` forms nest; `rel` is taken from whichever form nested.

- [ ] **Step 1: Failing tests** (each asserts exit 3, the offending value in stderr, and that no file was deleted where `--delete --yes` is passed)

- `test_cli_exclude_dir_nonexistent_refuses`: `--exclude-dir nope`.
- `test_cli_exclude_dir_doubled_path_refuses_and_deletes_nothing`: results has `keep/x.csv`; run from `tmp_path` with `--exclude-dir results/keep --delete --yes`; exit 3 and `keep/x.csv` still exists.
- `test_cli_exclude_dir_file_refuses`: value names a regular file.
- `test_cli_exclude_dir_outside_results_refuses`: absolute path to a sibling directory.
- `test_cli_exclude_dir_is_results_dir_refuses`: `--exclude-dir .`.
- `test_cli_exclude_dir_symlink_refuses`: `results/link -> elsewhere`.
- Review Focus 3 — `test_cli_exclude_dir_realpath_under_symlinked_results_prunes`: `link -> results`; scan `link` with `--exclude-dir <realpath of results>/keep`; exit 0, `keep/x.csv` not in stdout, "Excluded 1 directory" in stderr.
- Existing passing behaviour stays: relative `keep` and absolute `<results>/keep` both prune.

- [ ] **Step 2:** Run; expect the refusal tests FAIL (exit 0) and the doubled-path test to show the file deleted.
- [ ] **Step 3:** Implement; call it with the other early guards (before the workflow loads); on `ExcludeDirError` echo `Refusing to scan: --exclude-dir `<value>` (resolved to `<path>`) <reason>. Relative paths resolve against the results directory.` and exit 3. Update the option help text. The walker and its `…_nonexistent_is_noop` unit test are unchanged.
- [ ] **Step 4:** Full suite passes (update any CLI test that passed a non-existent exclusion).
- [ ] **Step 5:** Commit `fix(cli): refuse --exclude-dir values that are not directories inside the results dir`.

### Task 8: `**/` in `--ignore` matches zero directories (F3)

**Files:** Modify `src/snakeprune/walker.py`; Test `tests/test_walker.py`.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.parametrize("rel,glob,expected", [
    ("sample.bam.bai", "**/*.bai", True),
    ("a/b/sample.bam.bai", "**/*.bai", True),
    ("a/b.txt", "a/**/b.txt", True),
    ("a/x/y/b.txt", "a/**/b.txt", True),
    ("sample.bam", "**/*.bai", False),
    ("b/a/b.txt", "a/**/b.txt", False),
    ("sub/x.txt", "sub/**", True),
    ("a/b/c.log", "*.log", True),   # `*` still crosses `/`
])
def test_ignore_glob_semantics(rel, glob, expected):
    assert _matches_any_glob(rel, [glob]) is expected
```

- [ ] **Step 2:** Run; expect rows 1 and 3 FAIL.
- [ ] **Step 3:** Expand each glob once, up front in `iter_results_files`, into itself plus every variant with one or more `**/` occurrences removed; match against the expanded tuple. Keep the existing `sub/**` handling.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(walker): **/ in --ignore globs also matches zero directories`.

### Task 9: Count symlinked directories skipped under `--follow-symlinks` (F8)

**Files:** Modify `src/snakeprune/walker.py`, `src/snakeprune/cli.py` (help text); Test `tests/test_walker.py`, `tests/test_cli.py`.

- [ ] **Step 1: Failing tests**

`test_iter_results_files_counts_skipped_symlinked_dirs_when_following`: `results/big -> tmp_path/ext` (a directory holding `f.txt`) plus a symlinked *file*; walk with `follow_symlinks=True, stats=stats`; assert `stats["skipped_symlinked_dirs"] == 1`, the symlinked file is yielded, and nothing under `big/` is.

`test_cli_follow_symlinks_reports_skipped_symlinked_dirs`: same layout via the CLI with `--follow-symlinks`; `Skipped 1 symlinked subdirectory` in stderr.

- [ ] **Step 2:** Run; expect FAIL (count 0).
- [ ] **Step 3:** Increment the counter for every symlink-to-directory entry in both modes (dangling links and `OSError` are still ignored). Change the flag help to `Include symlinked files (symlinked directories are never descended into).` and update the walker docstring.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(walker): report symlinked directories skipped under --follow-symlinks`.

### Task 10: Pre-validate trash target ancestors (F6)

**Files:** Modify `src/snakeprune/delete.py`; Test `tests/test_delete.py`, `tests/test_cli.py`.

- [ ] **Step 1: Failing tests**

`test_delete_orphans_trash_refuses_when_ancestor_is_a_file`: trash holds a regular file at `trash/results/a`; orphans `[z.txt, a/b.txt]` in that order; `pytest.raises(FileExistsError)`; both source files still exist and `trash/results/z.txt` does not.

`test_delete_orphans_trash_allows_existing_ancestor_directory`: trash holds directory `trash/results/a`; orphan `a/b.txt` moves successfully.

`test_cli_trash_ancestor_file_refuses_with_exit_3`: same layout via `--trash … --yes`; exit 3, sources intact.

- [ ] **Step 2:** Run; expect the first FAIL with `DeletionFailed` after `z.txt` was moved.
- [ ] **Step 3:** In the validation loop, for each ancestor of `target` below `trash_dir`, raise `FileExistsError` (`Refusing to move <path> to trash: <ancestor> exists and is not a directory. Empty or rotate the trash directory, or choose another.`) if it exists (or is a dangling symlink) and is not a directory. Cache checked ancestors in a set so a large batch does not re-stat shared parents.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5:** Commit `fix(delete): refuse up front when a trash target's ancestor is a file`.

### Task 11: Documentation

**Files:** Modify `README.md`, `docs/test-coverage.md`.

- [ ] **Step 1:** README: `--exclude-dir` must name an existing directory inside the results dir, relative to the results dir, else exit 3; `--ignore "**/*.bai"` now covers the top level, and `*` crosses `/`; `--follow-symlinks` covers symlinked files only and skipped directories are reported; nested results dirs are matched by their path relative to the workflow's working directory (basename when outside it); Snakefiles that print are supported; unnormalised rule paths are normalised and an unresolvable `..` warns.
- [ ] **Step 2:** `docs/test-coverage.md`: add the new tests under the existing headings and refresh every count from `python -m pytest --collect-only -q | tail -1` and per-file `grep -c "^def test_"`. State the counts you measured; do not estimate.
- [ ] **Step 3:** Full suite passes; commit `docs: document round-2 review fixes and refresh test counts`.
