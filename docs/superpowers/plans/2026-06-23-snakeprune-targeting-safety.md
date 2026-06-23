# snakeprune Targeting Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two target-only safety features to `snakeprune scan`: a naughty-directory guard (warn always, refuse on delete) and a walk-pruning `--exclude-dir` option.

**Architecture:** Both checks are cheap and need no rule information. The naughty-dir guard runs first thing in `cli.scan` (before the workflow subprocess is spawned). `--exclude-dir` is threaded into `walker.iter_results_files` so excluded subtrees are never descended into. Both reuse existing conventions: exit code 3 for refusals, a per-feature override flag, warnings printed via `typer.echo(..., err=True)` (so they survive `--quiet`), and a one-line stderr tally for visibility.

**Tech Stack:** Python 3, Typer CLI, pytest + `typer.testing.CliRunner`, `os.scandir`-based walker.

## Global Constraints

- snakeprune itself stays pure-stdlib — **no new third-party dependencies**. (`os`, `pathlib` only.)
- Refusals exit with **code 3**, consistent with the existing safety gates.
- Safety warnings and tallies are printed with `typer.echo(..., err=True)` **directly, never via the `log()` helper**, so they appear even under `--quiet`.
- Naughty-dir matching is a **case-sensitive exact match** on `results_dir.name`.
- Path matching for `--exclude-dir` uses `os.path.abspath` on **both** sides (never `os.path.realpath`), so the two derive from the same base and symlink resolution can't cause a mismatch.
- Changes are purely additive: no existing flag changes meaning.

---

### Task 1: Walker `--exclude-dir` support (prune subtrees from the walk)

**Files:**
- Modify: `src/snakeprune/walker.py` (`iter_results_files`, lines 22-93)
- Test: `tests/test_walker.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `iter_results_files(results_dir, ignore_globs=(), follow_symlinks=False, exclude_dirs=(), stats=None)` — new `exclude_dirs: Iterable[str]` keyword (absolute or relative path strings; normalised internally with `os.path.abspath`). When `stats` is provided it now also initialises and increments `stats["excluded_dirs"]` (in addition to the existing `stats["skipped_symlinked_dirs"]`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_walker.py`:

```python
def test_iter_results_files_exclude_dirs_prunes_subtree(make_results):
    results = make_results(["keep/a.txt", "sub/b.txt"])
    exclude = os.path.abspath(results / "keep")
    rels = sorted(rel for _, rel in iter_results_files(results, exclude_dirs=[exclude]))
    assert rels == ["sub/b.txt"]


def test_iter_results_files_exclude_dirs_nonexistent_is_noop(make_results):
    results = make_results(["a.txt", "sub/b.txt"])
    exclude = os.path.abspath(results / "does_not_exist")
    rels = sorted(rel for _, rel in iter_results_files(results, exclude_dirs=[exclude]))
    assert rels == ["a.txt", "sub/b.txt"]


def test_iter_results_files_exclude_dirs_counts_in_stats(make_results):
    results = make_results(["keep/a.txt", "sub/b.txt"])
    exclude = os.path.abspath(results / "keep")
    stats: dict = {}
    list(iter_results_files(results, exclude_dirs=[exclude], stats=stats))
    assert stats == {"skipped_symlinked_dirs": 0, "excluded_dirs": 1}
```

Also update the two existing `stats` assertions in `tests/test_walker.py` to include the new key (the stats contract grew):

- In `test_iter_results_files_stats_counts_skipped_symlinked_dirs`, change
  `assert stats == {"skipped_symlinked_dirs": 1}`
  to
  `assert stats == {"skipped_symlinked_dirs": 1, "excluded_dirs": 0}`
- In `test_iter_results_files_stats_zero_when_no_dir_symlinks`, change
  `assert stats == {"skipped_symlinked_dirs": 0}`
  to
  `assert stats == {"skipped_symlinked_dirs": 0, "excluded_dirs": 0}`

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_walker.py -k exclude_dirs -v`
Expected: FAIL — `iter_results_files() got an unexpected keyword argument 'exclude_dirs'`

- [ ] **Step 3: Implement the walker change**

In `src/snakeprune/walker.py`, change the signature of `iter_results_files` to add `exclude_dirs` (between `follow_symlinks` and `stats`):

```python
def iter_results_files(
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
    exclude_dirs: Iterable[str] = (),
    stats: dict | None = None,
) -> Iterator[tuple[str, str]]:
```

Just after `ignore_globs = tuple(ignore_globs)` (line 46), add the exclude-set normalisation and the new stats key:

```python
    ignore_globs = tuple(ignore_globs)
    exclude_set = {os.path.abspath(p) for p in exclude_dirs}
    if stats is not None:
        stats["skipped_symlinked_dirs"] = 0
        stats["excluded_dirs"] = 0
```

In the directory-descent branch (currently lines 80-82), add the exclude check before pushing onto the stack:

```python
                    if not is_link and entry.is_dir(follow_symlinks=False):
                        if exclude_set and os.path.abspath(entry.path) in exclude_set:
                            if stats is not None:
                                stats["excluded_dirs"] += 1
                            continue
                        stack.append(entry.path)
                        continue
```

(`os` is already imported at the top of `walker.py`.)

- [ ] **Step 4: Run the walker tests to verify they pass**

Run: `pytest tests/test_walker.py -v`
Expected: PASS (all walker tests, including the two updated stats assertions).

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "feat(walker): --exclude-dir prunes subtrees from the scan"
```

---

### Task 2: Naughty-directory guard in the CLI

**Files:**
- Modify: `src/snakeprune/cli.py` (module constant near top; new options on `scan`; guard block at the start of the `scan` body)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: module constant `DEFAULT_NAUGHTY_DIRS: frozenset[str]`; two new `scan` options `--naughty-dir NAME` (repeatable, `Optional[list[str]]`) and `--allow-naughty-dir` (bool). The guard runs before the workflow is loaded.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_naughty_dir_dry_run_warns_but_proceeds(make_pipeline, tmp_path):
    # Rule writes under `resources/` so the basename-mismatch refusal does NOT
    # fire; this isolates the naughty-dir warning on a clean dry-run.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'resources/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "obsolete.csv").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(resources)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "resources" in combined
    assert "input/config directory" in combined
    # Dry-run still lists the orphan.
    assert "obsolete.csv" in result.stdout


def test_cli_scan_naughty_dir_delete_refuses(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(config), "--delete", "--yes"]
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--allow-naughty-dir" in combined
    # Refusal happens before the workflow is even loaded.
    assert "Loading Snakemake workflow" not in combined
    # Nothing deleted.
    assert (config / "obsolete.csv").exists()


def test_cli_scan_naughty_dir_delete_allowed_proceeds(make_pipeline, tmp_path):
    # Rule writes under `config/` so basename-mismatch does not interfere;
    # --allow-naughty-dir lets the delete flow run.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'config/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(config),
            "--delete", "--yes", "--allow-naughty-dir", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 0
    assert not (config / "obsolete.csv").exists()


def test_cli_scan_naughty_dir_custom_via_flag(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    custom = tmp_path / "custom_inputs"
    custom.mkdir()
    (custom / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(custom), "--delete", "--yes", "--naughty-dir", "custom_inputs"],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "custom_inputs" in combined
    assert "--allow-naughty-dir" in combined


def test_cli_scan_non_naughty_dir_no_warning(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "input/config directory" not in combined
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_cli.py -k naughty -v`
Expected: FAIL — `--naughty-dir`/`--allow-naughty-dir` are unrecognised options (Typer exits 2), and the warning text is absent.

- [ ] **Step 3: Add the constant and the CLI options**

In `src/snakeprune/cli.py`, add the module-level constant just below `PROGRESS_INTERVAL = 10000` (line 24):

```python
DEFAULT_NAUGHTY_DIRS = frozenset(
    {"resources", "config", "profile", "workflow", ".snakemake"}
)
```

Add two options to the `scan` signature (place them alongside the other options, e.g. just before the `limit` option at line 86):

```python
    naughty_dir: Optional[list[str]] = typer.Option(
        None,
        "--naughty-dir",
        help="Extra directory basename to treat as a suspicious target; repeatable.",
    ),
    allow_naughty_dir: bool = typer.Option(
        False,
        "--allow-naughty-dir",
        help="Bypass the delete/trash refusal when the target dir basename is on the naughty list.",
    ),
```

- [ ] **Step 4: Add the guard block at the top of `scan`**

In `src/snakeprune/cli.py`, insert the guard immediately after the `log` helper definition (lines 90-92) and **before** `log(f"Loading Snakemake workflow from {pipeline_dir}...")` (line 94):

```python
    naughty = DEFAULT_NAUGHTY_DIRS | set(naughty_dir or ())
    if results_dir.name in naughty:
        typer.echo(
            f"WARNING: `{results_dir.name}` is a conventional Snakemake "
            f"input/config directory and is unlikely to contain prunable rule "
            f"outputs. Review the orphan list carefully — this may be the wrong "
            f"target.",
            err=True,
        )
        if (delete or trash is not None) and not allow_naughty_dir:
            typer.echo(
                f"Refusing to delete: `{results_dir.name}` looks like a "
                f"Snakemake input/config directory. Pass --allow-naughty-dir "
                f"to override.",
                err=True,
            )
            raise typer.Exit(code=3)
```

- [ ] **Step 5: Run the naughty tests to verify they pass**

Run: `pytest tests/test_cli.py -k naughty -v`
Expected: PASS (all five naughty tests).

- [ ] **Step 6: Run the full CLI suite to check for regressions**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (no existing test regressed).

- [ ] **Step 7: Commit**

```bash
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): naughty-dir guard warns on scan, refuses on delete"
```

---

### Task 3: Wire `--exclude-dir` into the CLI

**Files:**
- Modify: `src/snakeprune/cli.py` (`import os`; new `--exclude-dir` option; resolve paths; pass to walker; print tally)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `iter_results_files(..., exclude_dirs=...)` and `stats["excluded_dirs"]` from Task 1.
- Produces: new `scan` option `--exclude-dir PATH` (repeatable, `Optional[list[str]]`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_exclude_dir_relative_prunes_subtree(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 'keep/old.csv' would be an orphan, but is excluded; '1.txt' is live.
    results = make_results(["1.txt", "keep/old.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--exclude-dir", "keep"]
    )
    assert result.exit_code == 0
    assert "old.csv" not in result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "Excluded 1 directory subtree" in combined


def test_cli_scan_exclude_dir_absolute_path(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "keep/old.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--exclude-dir", str(results / "keep")],
    )
    assert result.exit_code == 0
    assert "old.csv" not in result.stdout
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_cli.py -k exclude_dir -v`
Expected: FAIL — `--exclude-dir` is an unrecognised option (Typer exits 2), so `old.csv` is still listed.

- [ ] **Step 3: Add the `os` import and the `--exclude-dir` option**

In `src/snakeprune/cli.py`, add `import os` to the imports at the top (after `import sys`, line 4):

```python
import os
import sys
```

Add the option to the `scan` signature (next to `--naughty-dir`, before `limit`):

```python
    exclude_dir: Optional[list[str]] = typer.Option(
        None,
        "--exclude-dir",
        help="Directory subtree to skip entirely; repeatable. Relative paths "
             "resolve against results_dir.",
    ),
```

- [ ] **Step 4: Resolve exclude paths and pass them to the walker**

In `src/snakeprune/cli.py`, just before the walk loop (before `for full_path, rel in iter_results_files(` at line 137), build the resolved exclude set:

```python
    exclude_set: set[str] = set()
    for d in exclude_dir or ():
        p = Path(d)
        resolved = p if p.is_absolute() else results_dir / p
        exclude_set.add(os.path.abspath(resolved))
```

Update the `iter_results_files(...)` call to pass `exclude_dirs`:

```python
    for full_path, rel in iter_results_files(
        results_dir,
        ignore_globs=tuple(ignore or ()),
        follow_symlinks=follow_symlinks,
        exclude_dirs=tuple(exclude_set),
        stats=walk_stats,
    ):
```

- [ ] **Step 5: Print the excluded-dir tally**

In `src/snakeprune/cli.py`, immediately after the skipped-symlinked-dirs block (after line 162, the `typer.echo(...)` for skipped dirs), add:

```python
    excluded_dirs = walk_stats.get("excluded_dirs", 0)
    if excluded_dirs > 0:
        typer.echo(
            f"Excluded {excluded_dirs} directory subtree(s) from the scan.",
            err=True,
        )
```

- [ ] **Step 6: Run the exclude-dir tests to verify they pass**

Run: `pytest tests/test_cli.py -k exclude_dir -v`
Expected: PASS (both exclude-dir tests).

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS (entire suite green).

- [ ] **Step 8: Commit**

```bash
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): --exclude-dir wired through to the walker with a tally"
```

---

### Task 4: README documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the user-facing behaviour from Tasks 1-3.
- Produces: documentation only (no code, no tests).

- [ ] **Step 1: Add the naughty-dir bullet under "Built-in refusals"**

In `README.md`, under **Safety and limitations → Built-in refusals** (after the "High orphan rate." bullet, currently line 69), add:

```markdown
- **Naughty target directories.** Targeting a conventional Snakemake input/config directory (`resources`, `config`, `profile`, `workflow`, `.snakemake`) prints a warning, and — under `--delete` / `--trash` — refuses unless `--allow-naughty-dir` is passed. The check fires before the workflow is even loaded, and especially guards against pointing at `resources/` (downloaded reference data). Extend the list for a run with `--naughty-dir NAME` (repeatable).
```

- [ ] **Step 2: Add an `--exclude-dir` usage example**

In `README.md`, in the **Usage** code block (after the `--ignore` example, currently lines 43-44), add:

```bash
# Skip an entire subtree from the walk (faster than --ignore for big dirs)
snakeprune scan path/to/pipeline path/to/results --exclude-dir old_results_to_keep
```

- [ ] **Step 3: Note the `--exclude-dir` vs `--ignore` distinction**

In `README.md`, under **Safety and limitations → Limitations** (or immediately after the Usage block — implementer's choice for the most natural fit), add a short clarifying sentence:

```markdown
`--exclude-dir DIR` prunes a whole subtree from the walk (the directory is never descended into), which is faster than `--ignore "DIR/**"` for large directories you want to protect. `--ignore` remains for file-level glob filtering. Relative `--exclude-dir` paths resolve against the results directory.
```

- [ ] **Step 4: Verify the README renders and reads correctly**

Run: `git diff README.md`
Expected: the three additions appear in the right sections with valid Markdown.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(README): naughty-dir guard and --exclude-dir"
```

---

## Self-Review

**Spec coverage:**
- Naughty-dir guard (warn always / refuse on delete / `--allow-naughty-dir` / `--naughty-dir` / runs before load / case-sensitive exact match) → Task 2.
- `DEFAULT_NAUGHTY_DIRS = {resources, config, profile, workflow, .snakemake}`, `logs`/`benchmarks` excluded → Task 2 (constant).
- `--exclude-dir` walk-pruning, relative-vs-absolute resolution, tally line, non-existent = no-op → Task 1 (walker) + Task 3 (CLI wiring).
- High-orphan-rate denominator excludes pruned files → satisfied for free: excluded files are never yielded, so `file_count` never counts them (no code needed; covered by Task 1's pruning).
- README updates → Task 4.

**Placeholder scan:** No TBD/TODO; every code step shows real code; every test step shows real assertions.

**Type consistency:** `exclude_dirs` is the keyword name in both the walker signature (Task 1) and the CLI call (Task 3). `stats["excluded_dirs"]` is the key written in Task 1 and read in Task 3. `DEFAULT_NAUGHTY_DIRS`, `--naughty-dir`, `--allow-naughty-dir`, `--exclude-dir` spelled consistently across tasks. The `os` import is added in Task 3 (the first CLI task that needs it); Task 2 uses no `os`.
