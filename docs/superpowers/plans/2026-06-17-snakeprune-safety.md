# snakeprune Safety & Pre-Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add layered safety to snakeprune (pre-scan refusals, post-scan rate gate, interactive `--delete` confirmation, opt-in trash mode, skipped-symlink-dir visibility) plus matching README documentation, so the tool can be released publicly without footguns.

**Architecture:** Each safety check is independent and individually bypassable. Pre-scan refusals (empty rule list, basename mismatch) run after pattern loading but before the walk. The walk gains optional `stats` for visibility into skipped symlinks. After the walk, a high-orphan-rate gate runs. The deletion flow shows a summary, gates on TTY + confirmation, then dispatches to either unlink or trash-move depending on `--trash`.

**Tech Stack:** Python 3.12+, Typer, pytest, Snakemake API, `os.scandir`, `shutil.move`.

## Global Constraints

- Python type hints throughout (`from __future__ import annotations` is already standard in this project).
- Test-Driven Development: every behaviour change starts with a failing test.
- New tests go in the existing `tests/test_<module>.py` files; no new test files.
- CLI tests use `typer.testing.CliRunner`; the runner sets stdin to a non-TTY StringIO by default — tests that need to simulate a TTY use `monkeypatch.setattr("sys.stdin.isatty", lambda: True)`.
- Each task ends with one commit and one `git push` to `origin/main`.
- Exit code conventions: 0 = success / clean abort, 2 = pre-existing usage errors (e.g. no Snakefile), 3 = safety refusal added by this plan.
- Preserve existing public API: `iter_results_files` and `OrphanFile` may grow fields/kwargs but must remain backwards-compatible for callers that don't use them.

---

### Task 1: Walker `stats` kwarg for skipped symlinked directories

**Files:**
- Modify: `src/snakeprune/walker.py` (function `iter_results_files`)
- Modify: `tests/test_walker.py` (add tests after existing walker tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `iter_results_files(..., stats: dict | None = None)`. When `stats` is a dict, the walker sets `stats["skipped_symlinked_dirs"] = 0` on entry and increments it once per directory entry that is a symlink to a directory and is being skipped because `follow_symlinks=False`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_walker.py`, after the existing walker tests:

```python
def test_iter_results_files_stats_counts_skipped_symlinked_dirs(make_results, tmp_path):
    results = make_results(["a.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    stats: dict = {}
    rels = [rel for _, rel in iter_results_files(results, stats=stats)]
    assert "a.txt" in rels
    # The symlinked directory itself was skipped, and its contents were not visited.
    assert "link_dir/x.txt" not in rels
    assert stats == {"skipped_symlinked_dirs": 1}


def test_iter_results_files_stats_zero_when_no_dir_symlinks(make_results):
    results = make_results(["a.txt", "sub/b.txt"])
    stats: dict = {}
    list(iter_results_files(results, stats=stats))
    assert stats == {"skipped_symlinked_dirs": 0}


def test_iter_results_files_stats_omitted_does_not_error(make_results, tmp_path):
    # File-symlinks and dir-symlinks both present; default kwarg path must still work.
    results = make_results(["a.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    (results / "link_file").symlink_to(external / "x.txt")
    rels = [rel for _, rel in iter_results_files(results)]
    assert rels == ["a.txt"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_walker.py -k stats -v
```

Expected: `test_iter_results_files_stats_counts_skipped_symlinked_dirs` and `test_iter_results_files_stats_zero_when_no_dir_symlinks` FAIL with `TypeError: iter_results_files() got an unexpected keyword argument 'stats'`. The "omitted" test should PASS already.

- [ ] **Step 3: Implement the `stats` kwarg**

Edit `src/snakeprune/walker.py`. Update the `iter_results_files` signature and the skip-symlink branch:

Change the signature:
```python
def iter_results_files(
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
    stats: dict | None = None,
) -> Iterator[tuple[str, str]]:
```

Update the docstring's first paragraph to add a sentence at the end:
```
    If ``stats`` is provided, the walker initialises
    ``stats["skipped_symlinked_dirs"] = 0`` and increments it once per
    directory entry that is a symlink to a directory and is being skipped
    because ``follow_symlinks=False``.
```

Right after `ignore_globs = tuple(ignore_globs)`, add:
```python
    if stats is not None:
        stats["skipped_symlinked_dirs"] = 0
```

Replace the existing `if is_link and not follow_symlinks: continue` branch with:
```python
                if is_link and not follow_symlinks:
                    if stats is not None:
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                stats["skipped_symlinked_dirs"] += 1
                        except OSError:
                            pass
                    continue
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_walker.py -v
```

Expected: all walker tests PASS.

- [ ] **Step 5: Run the full suite**

```
python -m pytest -q
```

Expected: all tests PASS (no regressions).

- [ ] **Step 6: Commit and push**

```
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "feat(walker): optional stats kwarg counts skipped symlinked dirs"
git push
```

---

### Task 2: `OrphanFile.rel` field plumbed from walker to consumers

**Files:**
- Modify: `src/snakeprune/walker.py` (dataclass `OrphanFile`, function `find_orphans`)
- Modify: `src/snakeprune/cli.py` (scan loop)
- Modify: `tests/test_walker.py` (one new test on `find_orphans`)

**Interfaces:**
- Consumes: walker already yields `(full_path: str, rel_posix: str)` tuples.
- Produces: `OrphanFile(path: Path, rel: str, likely_rule: str | None = None)`. `rel` is the POSIX-style path of the orphan relative to the results dir — exactly what the walker computed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_walker.py`:

```python
def test_find_orphans_sets_rel_on_orphan_file(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete/3.txt"])
    orphans = find_orphans(pipeline, results)
    assert len(orphans) == 1
    assert orphans[0].rel == "obsolete/3.txt"
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_walker.py::test_find_orphans_sets_rel_on_orphan_file -v
```

Expected: FAIL with `TypeError: OrphanFile.__init__() got an unexpected keyword argument 'rel'` (or `AttributeError` once it's constructed without `rel`).

- [ ] **Step 3: Add `rel` to OrphanFile and update callers**

Edit `src/snakeprune/walker.py`. Update `OrphanFile`:

```python
@dataclass(frozen=True)
class OrphanFile:
    path: Path
    rel: str
    likely_rule: str | None = None
```

Update `find_orphans` to pass `rel`:

```python
    for full_path, rel in iter_results_files(
        results_dir, ignore_globs=ignore_globs, follow_symlinks=follow_symlinks
    ):
        match_target = target_prefix + rel
        if combined is not None and combined.match(match_target):
            continue
        likely = attribute_orphan_to_rule(match_target, patterns) if attribute_rules else None
        orphans.append(OrphanFile(path=Path(full_path), rel=rel, likely_rule=likely))
```

Edit `src/snakeprune/cli.py`. In the scan loop, update the `orphans.append(...)` line to:

```python
        orphans.append(OrphanFile(path=Path(full_path), rel=rel, likely_rule=likely))
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest -q
```

Expected: all PASS. (Existing `test_delete.py` tests construct `OrphanFile(path=...)` without `rel` and will fail.)

If `test_delete.py` tests fail with "missing argument: rel", update them to pass `rel=str(f.name)` (or any sensible string) — `rel` is unused by the unlink path so any value works. Concretely, change each `OrphanFile(path=...)` in `tests/test_delete.py` to `OrphanFile(path=..., rel="dummy")`.

Re-run:
```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 5: Commit and push**

```
git add src/snakeprune/walker.py src/snakeprune/cli.py tests/test_walker.py tests/test_delete.py
git commit -m "feat(walker): OrphanFile carries rel-posix path under results dir"
git push
```

---

### Task 3: Pre-scan refusal on empty rule list

**Files:**
- Modify: `src/snakeprune/cli.py` (add flag + refusal)
- Modify: `tests/test_cli.py` (two new tests)

**Interfaces:**
- Consumes: `find_rule_patterns()` return list from Task baseline.
- Produces: new flag `--allow-empty-rules` (Typer bool option, default False) on `snakeprune scan`. Refusal exits with code 3 and message printed to stderr.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_refuses_when_workflow_has_no_rules(tmp_path):
    # A workflow that defines no rules (just a configfile statement is enough
    # to make Snakemake load successfully but produce 0 rules).
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "Snakefile").write_text("# no rules here\n")
    results = tmp_path / "results"
    results.mkdir()
    (results / "anything.txt").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "0 output patterns" in combined
    assert "--allow-empty-rules" in combined


def test_cli_scan_allow_empty_rules_bypasses_refusal(tmp_path):
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "Snakefile").write_text("# no rules here\n")
    results = tmp_path / "results"
    results.mkdir()
    (results / "anything.txt").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--allow-empty-rules"]
    )
    assert result.exit_code == 0
    assert "anything.txt" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_cli.py -k "empty_rules" -v
```

Expected: both FAIL — first because exit code is 0 (everything reported as orphan), second because the flag doesn't exist.

- [ ] **Step 3: Add the flag and refusal**

Edit `src/snakeprune/cli.py`. Add the option in the `scan` signature, after `quiet`:

```python
    allow_empty_rules: bool = typer.Option(
        False,
        "--allow-empty-rules",
        help="Bypass refusal when the workflow has 0 output patterns.",
    ),
```

Right after the line `log(f"Loaded {len(patterns)} rule output pattern(s).")`, add:

```python
    if not patterns and not allow_empty_rules:
        typer.echo(
            "Workflow loaded but produced 0 output patterns. Refusing to "
            "scan: every file would be reported as an orphan. Check that "
            "the right config / env is loaded, or pass --allow-empty-rules "
            "to override.",
            err=True,
        )
        raise typer.Exit(code=3)
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 5: Commit and push**

```
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): refuse to scan when workflow yields 0 rule patterns"
git push
```

---

### Task 4: Pre-scan refusal on results-dir basename / rule-prefix mismatch

**Files:**
- Modify: `src/snakeprune/patterns.py` (extract a public `extract_literal_prefix` helper)
- Modify: `src/snakeprune/walker.py` (refactor `attribute_orphan_to_rule` to use the new helper)
- Modify: `src/snakeprune/cli.py` (add flag + refusal)
- Modify: `tests/test_patterns.py` (test the new helper)
- Modify: `tests/test_cli.py` (two new tests)

**Interfaces:**
- Consumes: list of `(rule_name, compiled_regex)` from `find_rule_patterns`.
- Produces:
  - `patterns.extract_literal_prefix(compiled: re.Pattern) -> str` — returns the un-escaped literal prefix of an anchored rule regex (everything before the first capture group). Replaces the inline logic currently in `attribute_orphan_to_rule`.
  - New CLI flag `--allow-basename-mismatch`. Refusal exits 3 when no rule's literal prefix starts with `<results_dir.name>/`.

- [ ] **Step 1: Write the helper test first**

Add to `tests/test_patterns.py`:

```python
from snakeprune.patterns import extract_literal_prefix


def test_extract_literal_prefix_returns_path_before_first_wildcard():
    regex_str = wildcard_pattern_to_regex("results/qc/{sample}.txt", constraints={})
    pat = _re_module.compile(regex_str)
    assert extract_literal_prefix(pat) == "results/qc/"


def test_extract_literal_prefix_handles_pattern_without_wildcards():
    regex_str = wildcard_pattern_to_regex("results/static/file.txt", constraints={})
    pat = _re_module.compile(regex_str)
    # No capture group at all -- the whole literal up to '$' (minus escapes).
    assert extract_literal_prefix(pat) == "results/static/file.txt"
```

- [ ] **Step 2: Run helper tests to verify they fail**

```
python -m pytest tests/test_patterns.py -k extract_literal_prefix -v
```

Expected: FAIL with `ImportError: cannot import name 'extract_literal_prefix'`.

- [ ] **Step 3: Implement the helper and refactor**

Edit `src/snakeprune/patterns.py`. Add after the `combine_rule_patterns` definition:

```python
def extract_literal_prefix(compiled: re.Pattern) -> str:
    """Return the un-escaped literal prefix of an anchored rule regex.

    Reads ``compiled.pattern`` up to the first ``(`` (the first capture group)
    and reverses ``re.escape``-style backslash escaping so the result is a
    real path prefix. Returns the entire literal body (minus the trailing
    ``$``) if the pattern has no capture group.
    """
    body = compiled.pattern.lstrip("^")
    if body.endswith("$"):
        body = body[:-1]
    cut = body.find("(")
    literal = body if cut == -1 else body[:cut]
    return re.sub(r"\\(.)", r"\1", literal)
```

Edit `src/snakeprune/walker.py`. Replace the body of `attribute_orphan_to_rule` to use the helper:

```python
from snakeprune.patterns import (
    combine_rule_patterns,
    extract_literal_prefix,
    find_rule_patterns,
)


def attribute_orphan_to_rule(target: str, patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """Best-effort guess: the rule whose output pattern shares the longest literal
    prefix with `target`. Falls back to None if no rule shares a meaningful prefix.
    """
    best_rule: str | None = None
    best_prefix_len = 0
    for name, regex in patterns:
        prefix = extract_literal_prefix(regex)
        if target.startswith(prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_rule = name
    return best_rule
```

- [ ] **Step 4: Run helper + walker tests**

```
python -m pytest tests/test_patterns.py tests/test_walker.py -v
```

Expected: all PASS.

- [ ] **Step 5: Write the failing CLI tests**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_refuses_when_no_rule_writes_under_results_dir_basename(
    make_pipeline, tmp_path
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # User points at a directory whose basename ('outputs') doesn't appear in
    # any rule's literal prefix (rules write under 'results/').
    wrong = tmp_path / "outputs"
    wrong.mkdir()
    (wrong / "x.txt").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(wrong)])
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "outputs/" in combined
    assert "results/" in combined  # surfaced as the actual prefix
    assert "--allow-basename-mismatch" in combined


def test_cli_scan_allow_basename_mismatch_bypasses_refusal(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    wrong = tmp_path / "outputs"
    wrong.mkdir()
    (wrong / "x.txt").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(wrong), "--allow-basename-mismatch"]
    )
    assert result.exit_code == 0
    assert "x.txt" in result.stdout
```

- [ ] **Step 6: Run CLI tests to verify they fail**

```
python -m pytest tests/test_cli.py -k "basename_mismatch" -v
```

Expected: FAIL (no refusal yet, no flag).

- [ ] **Step 7: Add flag + refusal in cli.py**

Edit `src/snakeprune/cli.py`. Add the helper import:

```python
from snakeprune.patterns import (
    SnakefileNotFound,
    combine_rule_patterns,
    extract_literal_prefix,
    find_rule_patterns,
)
```

Add the flag to `scan` after `allow_empty_rules`:

```python
    allow_basename_mismatch: bool = typer.Option(
        False,
        "--allow-basename-mismatch",
        help="Bypass refusal when no rule writes under the results-dir basename.",
    ),
```

Right after the empty-rule refusal block, add:

```python
    if patterns and not allow_basename_mismatch:
        results_prefix = results_dir.name + "/"
        rule_prefixes = [extract_literal_prefix(p) for _, p in patterns]
        if not any(rp.startswith(results_prefix) for rp in rule_prefixes):
            # Surface up to 3 most common first-segments to help the user.
            from collections import Counter
            first_segs = Counter(rp.split("/", 1)[0] + "/" for rp in rule_prefixes if rp)
            top = ", ".join(f"`{seg}`" for seg, _ in first_segs.most_common(3))
            typer.echo(
                f"No rule writes under `{results_prefix}`. Rules write under: "
                f"{top}. Did you point at the wrong directory? Pass "
                f"--allow-basename-mismatch to override.",
                err=True,
            )
            raise typer.Exit(code=3)
```

- [ ] **Step 8: Run full suite**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 9: Commit and push**

```
git add src/snakeprune/patterns.py src/snakeprune/walker.py src/snakeprune/cli.py tests/test_patterns.py tests/test_cli.py
git commit -m "feat(cli): refuse on results-dir / rule-prefix basename mismatch"
git push
```

---

### Task 5: Post-scan high-orphan-rate warning + threshold + bypass

**Files:**
- Modify: `src/snakeprune/cli.py` (post-scan check + flags)
- Modify: `tests/test_cli.py` (three new tests)

**Interfaces:**
- Consumes: `file_count`, `len(orphans)` already computed in the scan loop.
- Produces:
  - New flag `--orphan-rate-threshold FLOAT` (default 0.5; pass 1.0 to disable).
  - New flag `--allow-high-orphan-rate` (default False).
  - Warning printed to stderr whenever `orphans/scanned > threshold` (regardless of `--delete`). Used as a gate by the deletion flow in Task 6.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_warns_when_orphan_rate_high(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 1 live ('1.txt') vs 3 orphans -> 75% rate.
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" in combined
    assert "75" in combined  # percentage in the warning


def test_cli_scan_no_warning_below_threshold(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 3 live vs 1 orphan -> 25% rate, well below default 50% threshold.
    results = make_results(["1.txt", "2.txt", "3.txt", "obs.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" not in combined


def test_cli_scan_threshold_flag_disables_check_at_one(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--orphan-rate-threshold", "1.0"]
    )
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" not in combined
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_cli.py -k "orphan_rate or rate_high or threshold_flag" -v
```

Expected: failures (no warning emitted, no flag).

- [ ] **Step 3: Add flags + warning logic**

Edit `src/snakeprune/cli.py`. Add flags after `allow_basename_mismatch`:

```python
    orphan_rate_threshold: float = typer.Option(
        0.5,
        "--orphan-rate-threshold",
        help="Fraction (0.0-1.0) above which the high-orphan-rate warning fires "
             "(default 0.5; pass 1.0 to disable).",
    ),
    allow_high_orphan_rate: bool = typer.Option(
        False,
        "--allow-high-orphan-rate",
        help="Bypass --delete refusal when orphan rate exceeds threshold.",
    ),
```

After the scan loop's `log(f"Scanned ...")` line, before the orphan listing, add:

```python
    high_rate = False
    if file_count > 0:
        rate = len(orphans) / file_count
        if rate > orphan_rate_threshold:
            high_rate = True
            pct = round(rate * 100)
            typer.echo(
                f"WARNING: {pct}% of scanned files are orphans "
                f"({len(orphans)} of {file_count}). This is unusually high and "
                f"usually indicates a config or environment problem rather than "
                f"real cleanup. Review the list carefully before deleting.",
                err=True,
            )
```

(We retain `high_rate` so the deletion flow in Task 6 can gate on it.)

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 5: Commit and push**

```
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): warn when orphan rate exceeds --orphan-rate-threshold"
git push
```

---

### Task 6: `--delete` confirmation flow (`--yes`, TTY gate, summary, high-rate refusal, symlink-skip surface)

**Files:**
- Modify: `src/snakeprune/cli.py` (deletion summary + prompt + flag + walker stats integration)
- Modify: `tests/test_cli.py` (multiple new tests; update existing `--delete` tests to pass `--yes`)

**Interfaces:**
- Consumes: `orphans` list, `high_rate` bool from Task 5, `allow_high_orphan_rate` from Task 5.
- Produces:
  - New flag `--yes` on `scan`.
  - Walker now invoked with a fresh `stats = {}` dict so we can print a skipped-symlinked-dirs summary.
  - Deletion flow gates: high-rate refusal (unless `--allow-high-orphan-rate`), non-TTY refusal (unless `--yes`), interactive confirmation otherwise.

- [ ] **Step 1: Update existing `--delete` tests to pass `--yes`**

In `tests/test_cli.py`, the existing test `test_cli_scan_delete_flag_unlinks` will break under the new flow. Update it to pass `--yes`:

```python
def test_cli_scan_delete_flag_unlinks(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--delete", "--yes"]
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()
```

Run the suite to confirm the existing test now passes the new requirement before any further changes:

```
python -m pytest tests/test_cli.py::test_cli_scan_delete_flag_unlinks -v
```

Expected: FAIL — the flag doesn't exist yet, so Typer errors with "no such option: --yes".

- [ ] **Step 2: Write the failing tests for the new behaviour**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_delete_non_tty_without_yes_refuses(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--delete"])
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--yes" in combined
    # Nothing deleted
    assert (results / "obsolete.csv").exists()


def test_cli_scan_delete_high_rate_refused_without_allow_flag(
    make_pipeline, make_results
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 75% orphan rate.
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--delete", "--yes"]
    )
    assert result.exit_code == 3
    # All orphans preserved.
    assert (results / "obs1.csv").exists()
    combined = result.stdout + (result.stderr or "")
    assert "--allow-high-orphan-rate" in combined


def test_cli_scan_delete_high_rate_proceeds_with_allow_flag(
    make_pipeline, make_results
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--delete", "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 0
    assert not (results / "obs1.csv").exists()


def test_cli_scan_delete_prompt_aborts_on_n(make_pipeline, make_results, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    # Simulate a TTY so the prompt branch is taken.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--delete"], input="n\n"
    )
    assert result.exit_code == 0
    assert (results / "obsolete.csv").exists()
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined


def test_cli_scan_delete_prompt_proceeds_on_y(make_pipeline, make_results, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--delete"], input="y\n"
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()


def test_cli_scan_surfaces_skipped_symlinked_dirs(make_pipeline, make_results, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Skipped 1 symlinked subdirectory" in combined
```

- [ ] **Step 3: Run tests to verify they fail**

```
python -m pytest tests/test_cli.py -k "delete or symlinked_subdir" -v
```

Expected: failures (no `--yes`, no prompt logic, no symlink-skip summary).

- [ ] **Step 4: Implement the deletion flow**

Edit `src/snakeprune/cli.py`.

Add the import at the top of the file:
```python
import sys
```

Add the `--yes` flag in the `scan` signature, after `allow_high_orphan_rate`:

```python
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive Y/N prompt before deleting. Required for non-TTY use.",
    ),
```

Change the `iter_results_files(...)` call in the scan loop to pass `stats`:

```python
    walk_stats: dict = {}
    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
        stats=walk_stats,
    ):
```

After the `log(f"Scanned ...")` line (and before the high-rate block from Task 5), add the symlinked-dir summary:

```python
    skipped_dirs = walk_stats.get("skipped_symlinked_dirs", 0)
    if skipped_dirs > 0:
        suffix = "y" if skipped_dirs == 1 else "ies"
        typer.echo(
            f"Skipped {skipped_dirs} symlinked subdirector{suffix}; files "
            f"reachable only via those paths were not scanned.",
            err=True,
        )
```

Replace the existing `if delete and orphans: delete_orphans(...)` block at the end of the function with the full deletion flow:

```python
    if delete and orphans:
        total_bytes = 0
        for o in orphans:
            try:
                total_bytes += o.path.stat().st_size
            except OSError:
                pass
        typer.echo(
            f"About to delete {len(orphans)} file(s), {total_bytes} byte(s).",
            err=True,
        )
        if high_rate and not allow_high_orphan_rate:
            typer.echo(
                "Refusing to delete: orphan rate exceeded threshold. Pass "
                "--allow-high-orphan-rate to override.",
                err=True,
            )
            raise typer.Exit(code=3)
        if not yes:
            if not sys.stdin.isatty():
                typer.echo(
                    "Refusing to delete: stdin is not a TTY. Pass --yes to "
                    "confirm in scripts.",
                    err=True,
                )
                raise typer.Exit(code=3)
            answer = typer.prompt("Proceed? [y/N]", default="n", show_default=False)
            if answer.strip().lower() not in {"y", "yes"}:
                typer.echo("Aborted.", err=True)
                raise typer.Exit(code=0)
        delete_orphans(orphans, allow_symlinks=allow_symlinks)
```

- [ ] **Step 5: Run the full suite**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 6: Commit and push**

```
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): interactive delete flow with --yes, TTY gate, high-rate refusal, and symlink-skip surface"
git push
```

---

### Task 7: `--trash DIR` mode in `delete.py` and CLI

**Files:**
- Modify: `src/snakeprune/delete.py` (add `trash_dir` + `results_dir_name` params)
- Modify: `src/snakeprune/cli.py` (add `--trash` flag; route to `delete_orphans`)
- Modify: `tests/test_delete.py` (trash tests)
- Modify: `tests/test_cli.py` (CLI integration)

**Interfaces:**
- Consumes: `OrphanFile.rel` (Task 2).
- Produces:
  - `delete_orphans(orphans, allow_symlinks=False, trash_dir: Path | None = None, results_dir_name: str | None = None)`. When `trash_dir is not None`, each orphan is moved to `trash_dir / results_dir_name / orphan.rel` (intermediate dirs created); `results_dir_name` is required in that case.
  - New CLI flag `--trash DIR` on `scan`. Passing it implies deletion mode (no need to also pass `--delete`).

- [ ] **Step 1: Write the failing delete-layer tests**

Add to `tests/test_delete.py`:

```python
def test_delete_orphans_trash_moves_files_to_dir(tmp_path):
    src = tmp_path / "results" / "sub"
    src.mkdir(parents=True)
    f = src / "x.txt"
    f.write_text("data")
    trash = tmp_path / "trash"
    delete_orphans(
        [OrphanFile(path=f, rel="sub/x.txt")],
        allow_symlinks=False,
        trash_dir=trash,
        results_dir_name="results",
    )
    # Original gone, file relocated with full rel structure under <trash>/<results_dir_name>/.
    assert not f.exists()
    assert (trash / "results" / "sub" / "x.txt").read_text() == "data"


def test_delete_orphans_trash_creates_target_dir_if_missing(tmp_path):
    src = tmp_path / "results"
    src.mkdir()
    f = src / "a.txt"
    f.write_text("data")
    trash = tmp_path / "does_not_exist_yet"
    delete_orphans(
        [OrphanFile(path=f, rel="a.txt")],
        trash_dir=trash,
        results_dir_name="results",
    )
    assert (trash / "results" / "a.txt").exists()


def test_delete_orphans_trash_refuses_symlink_without_flag(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "results" / "link.txt"
    link.parent.mkdir()
    link.symlink_to(target)
    trash = tmp_path / "trash"
    with pytest.raises(PermissionError):
        delete_orphans(
            [OrphanFile(path=link, rel="link.txt")],
            allow_symlinks=False,
            trash_dir=trash,
            results_dir_name="results",
        )
    assert link.is_symlink()


def test_delete_orphans_trash_requires_results_dir_name(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data")
    trash = tmp_path / "trash"
    with pytest.raises(ValueError):
        delete_orphans(
            [OrphanFile(path=f, rel="x.txt")],
            trash_dir=trash,
            results_dir_name=None,
        )
```

- [ ] **Step 2: Run delete-layer tests to verify they fail**

```
python -m pytest tests/test_delete.py -k trash -v
```

Expected: FAIL — `trash_dir` kwarg doesn't exist.

- [ ] **Step 3: Implement trash mode in `delete.py`**

Replace `src/snakeprune/delete.py` with:

```python
"""Safe deletion of orphan files."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Iterable

from snakeprune.walker import OrphanFile


def delete_orphans(
    orphans: Iterable[OrphanFile],
    allow_symlinks: bool = False,
    trash_dir: Path | None = None,
    results_dir_name: str | None = None,
) -> None:
    """Remove each orphan: either ``path.unlink()`` (default) or
    ``shutil.move`` into ``trash_dir / results_dir_name / orphan.rel`` when
    ``trash_dir`` is provided. Refuses to operate on directories. Refuses
    symlinks unless ``allow_symlinks=True``. Prints what's being removed to
    stderr.

    ``results_dir_name`` is required when ``trash_dir`` is provided so that a
    single trash dir can be reused across multiple results dirs without
    collisions.
    """
    if trash_dir is not None and results_dir_name is None:
        raise ValueError("results_dir_name is required when trash_dir is set")
    for orphan in orphans:
        path = orphan.path
        if path.is_dir() and not path.is_symlink():
            raise IsADirectoryError(f"Refusing to delete directory: {path}")
        if path.is_symlink() and not allow_symlinks:
            raise PermissionError(
                f"Refusing to delete symlink {path} without --allow-symlinks"
            )
        if trash_dir is not None:
            assert results_dir_name is not None  # for type-checkers
            target = trash_dir / results_dir_name / orphan.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"moving to trash: {path} -> {target}", file=sys.stderr)
            shutil.move(str(path), str(target))
        else:
            print(f"deleting: {path}", file=sys.stderr)
            path.unlink()
```

- [ ] **Step 4: Run delete-layer tests to verify they pass**

```
python -m pytest tests/test_delete.py -v
```

Expected: all PASS.

- [ ] **Step 5: Write the failing CLI test**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_trash_moves_orphan_to_dir(make_pipeline, make_results, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    trash = tmp_path / "trash"
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--trash", str(trash), "--yes"]
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()
    assert (trash / results.name / "obsolete.csv").exists()


def test_cli_scan_trash_implies_delete_mode(make_pipeline, make_results, tmp_path):
    # User passes --trash but not --delete; deletion should still happen.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    trash = tmp_path / "trash"
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--trash", str(trash), "--yes"]
    )
    assert result.exit_code == 0
    assert (trash / results.name / "obsolete.csv").exists()
```

- [ ] **Step 6: Run CLI test to verify it fails**

```
python -m pytest tests/test_cli.py -k trash -v
```

Expected: FAIL (flag doesn't exist).

- [ ] **Step 7: Wire `--trash` into the CLI**

Edit `src/snakeprune/cli.py`.

Add the option in the `scan` signature, after `yes`:

```python
    trash: Optional[Path] = typer.Option(
        None,
        "--trash",
        help="Move orphans to this directory instead of unlinking; implies delete mode.",
    ),
```

In the deletion flow block, change the gate so trash also triggers it, and pass the trash dir through:

Replace:
```python
    if delete and orphans:
```
with:
```python
    deletion_requested = delete or trash is not None
    if deletion_requested and orphans:
```

And replace the final `delete_orphans(orphans, allow_symlinks=allow_symlinks)` line with:
```python
        delete_orphans(
            orphans,
            allow_symlinks=allow_symlinks,
            trash_dir=trash,
            results_dir_name=results_dir.name if trash is not None else None,
        )
```

- [ ] **Step 8: Run the full suite**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 9: Commit and push**

```
git add src/snakeprune/delete.py src/snakeprune/cli.py tests/test_delete.py tests/test_cli.py
git commit -m "feat(cli): --trash DIR moves orphans instead of unlinking"
git push
```

---

### Task 8: README — Safety and Limitations section + new usage examples

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: all flags introduced in Tasks 3–7.
- Produces: a new `Safety and limitations` section between `Usage` and `Runtime requirements`, plus updated usage examples in the `Usage` section.

- [ ] **Step 1: Add new usage examples**

Edit `README.md`. In the `Usage` section, add two new examples after the existing `--delete` example:

```bash
# Non-interactive delete (for scripts)
snakeprune scan path/to/pipeline path/to/results --delete --yes

# Reversible delete: move orphans into a trash directory instead of unlinking
snakeprune scan path/to/pipeline path/to/results --trash path/to/trash
```

- [ ] **Step 2: Add the Safety and limitations section**

Insert immediately after the closing line of the `Usage` section and immediately before the `## Runtime requirements` heading:

```markdown
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
```

- [ ] **Step 3: Verify the README renders sensibly**

Run a quick visual check (open in an editor or use a markdown viewer); ensure the section sits cleanly between `Usage` and `Runtime requirements`.

- [ ] **Step 4: Run the full suite (sanity check)**

```
python -m pytest -q
```

Expected: all PASS (no code changed, but verify nothing's broken).

- [ ] **Step 5: Commit and push**

```
git add README.md
git commit -m "docs(README): Safety and limitations section + new usage examples"
git push
```

---

## Self-Review

**Spec coverage:**
- Empty-rule refusal → Task 3 ✓
- Basename-mismatch refusal → Task 4 ✓
- High-orphan-rate warning + gate → Task 5 (warning) + Task 6 (delete gate) ✓
- `--delete` confirmation flow (TTY gate, `--yes`) → Task 6 ✓
- Skipped-symlink-dir tally → Task 1 (walker) + Task 6 (surface) ✓
- `--trash DIR` → Task 7 ✓
- `OrphanFile.rel` field → Task 2 ✓
- README updates → Task 8 ✓
- Backwards-compatibility note: tests for existing `--delete` invocations updated in Task 6 ✓

**Placeholder scan:** No TBDs, no "add appropriate handling" lines, every code step has the actual code.

**Type consistency:**
- `OrphanFile(path: Path, rel: str, likely_rule: str | None = None)` — used identically in walker, CLI, and `delete.py`.
- `delete_orphans(orphans, allow_symlinks, trash_dir, results_dir_name)` — same signature in implementation and tests.
- `iter_results_files(..., stats: dict | None = None)` — same in implementation and tests.
- `extract_literal_prefix(compiled: re.Pattern) -> str` — used in `patterns.py` (definition), `walker.attribute_orphan_to_rule`, and `cli.scan`.
- `--orphan-rate-threshold` value flows from CLI option (float) directly into `if rate > orphan_rate_threshold` — consistent.

Plan complete and saved to `docs/superpowers/plans/2026-06-17-snakeprune-safety.md`.
