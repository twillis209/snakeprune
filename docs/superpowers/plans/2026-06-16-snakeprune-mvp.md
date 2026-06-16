# snakeprune MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build snakeprune — a CLI that scans a Snakemake project's `results/` tree and reports files that no rule in the current pipeline can produce.

**Architecture:** Use Snakemake's Python API to load the workflow and enumerate every rule's `output:` patterns. Convert each `{wildcard}` placeholder to a regex (using rule-local then workflow-global `wildcard_constraints`, falling back to `[^/]+`). Walk `results/` and report files matching no pattern. Deletion behind an explicit flag.

**Tech Stack:** Python 3.12+, snakemake>=8.0 (Python API), Typer (CLI), pytest (tests). Build with hatchling.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/snakeprune/__init__.py` | Package marker; exposes `__version__` (already present). |
| `src/snakeprune/patterns.py` | Snakefile resolution, workflow loading, wildcard → regex conversion, compiled pattern list. |
| `src/snakeprune/walker.py` | Walk `results/`, apply ignore-globs, match against patterns, return orphans. |
| `src/snakeprune/delete.py` | Safe unlink of orphan files (regular files only, one at a time). |
| `src/snakeprune/cli.py` | Typer CLI: `scan` subcommand wiring everything together. |
| `tests/conftest.py` | Pytest fixture helpers: synthetic pipeline + synthetic results directory. |
| `tests/test_patterns.py` | Tests for wildcard → regex, Snakefile resolution, pattern loading. |
| `tests/test_walker.py` | Tests for file enumeration, ignore globs, orphan detection, rule attribution. |
| `tests/test_delete.py` | Tests for safe deletion (regular files only, symlink refusal). |
| `tests/test_cli.py` | End-to-end CLI test via Typer's CliRunner. |

Test fixtures live inline in tests (built via the `conftest.py` helpers) rather than committed under `tests/fixtures/`, so tests are self-contained.

---

## Task 1: Test fixture helpers (conftest)

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the conftest helpers**

```python
# tests/conftest.py
from pathlib import Path
import pytest


def _make_pipeline(tmp_path: Path, snakefile_text: str, smk_files: dict[str, str] | None = None) -> Path:
    """Create a synthetic pipeline directory with given Snakefile content and optional .smk files."""
    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    (pipeline_dir / "Snakefile").write_text(snakefile_text)
    for name, content in (smk_files or {}).items():
        target = pipeline_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pipeline_dir


def _make_results(tmp_path: Path, files: list[str]) -> Path:
    """Create a synthetic results directory with the given relative file paths (all empty)."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    for rel in files:
        full = results_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.touch()
    return results_dir


@pytest.fixture
def make_pipeline(tmp_path):
    def _factory(snakefile_text: str, smk_files: dict[str, str] | None = None) -> Path:
        return _make_pipeline(tmp_path, snakefile_text, smk_files)
    return _factory


@pytest.fixture
def make_results(tmp_path):
    def _factory(files: list[str]) -> Path:
        return _make_results(tmp_path, files)
    return _factory
```

- [ ] **Step 2: Add a smoke test to confirm fixtures work**

Add to `tests/test_patterns.py` (create the file):

```python
# tests/test_patterns.py
def test_make_pipeline_writes_snakefile(make_pipeline):
    pipeline = make_pipeline("rule all:\n    input: 'results/x.txt'\n")
    assert (pipeline / "Snakefile").read_text().startswith("rule all:")


def test_make_results_creates_files(make_results):
    results = make_results(["a/b.txt", "c/d/e.csv"])
    assert (results / "a" / "b.txt").exists()
    assert (results / "c" / "d" / "e.csv").exists()
```

- [ ] **Step 3: Install dependencies and run the smoke tests**

```bash
cd ~/projects/snakeprune
pip install -e ".[dev]"
pytest tests/test_patterns.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/snakeprune
git add tests/conftest.py tests/test_patterns.py
git commit -m "test: add pipeline and results fixture helpers"
```

---

## Task 2: Wildcard → regex conversion

**Files:**
- Create: `src/snakeprune/patterns.py`
- Modify: `tests/test_patterns.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_patterns.py`:

```python
from snakeprune.patterns import wildcard_pattern_to_regex


def test_no_wildcards_returns_anchored_literal():
    regex = wildcard_pattern_to_regex("results/x.txt", constraints={})
    assert regex == r"^results/x\.txt$"


def test_single_wildcard_uses_default_constraint():
    regex = wildcard_pattern_to_regex("results/{name}/x.txt", constraints={})
    # default [^/]+ for unconstrained wildcard
    assert regex == r"^results/(?P<name>[^/]+)/x\.txt$"


def test_wildcard_with_constraint():
    regex = wildcard_pattern_to_regex("results/{n}.txt", constraints={"n": r"\d+"})
    assert regex == r"^results/(?P<n>\d+)\.txt$"


def test_multiple_wildcards_each_constrained_independently():
    regex = wildcard_pattern_to_regex(
        "results/{a}/{b}.csv",
        constraints={"a": "x|y", "b": r"\d+"},
    )
    assert regex == r"^results/(?P<a>x|y)/(?P<b>\d+)\.csv$"


def test_regex_special_characters_in_literal_are_escaped():
    # dots, plus signs, brackets in the literal portion must be escaped
    regex = wildcard_pattern_to_regex("results/file.v1+x[y]/{n}.txt", constraints={})
    assert regex == r"^results/file\.v1\+x\[y\]/(?P<n>[^/]+)\.txt$"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 5 failures with `ImportError` or `AttributeError` for `wildcard_pattern_to_regex`.

- [ ] **Step 3: Implement `wildcard_pattern_to_regex`**

Create `src/snakeprune/patterns.py`:

```python
"""Build regexes from Snakemake rule output patterns."""
from __future__ import annotations

import re

_WILDCARD_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")


def wildcard_pattern_to_regex(pattern: str, constraints: dict[str, str]) -> str:
    """Convert a Snakemake output pattern to an anchored regex string.

    Each {wildcard} placeholder is replaced with a named capture group whose body
    is taken from `constraints[wildcard]` if present, else the default `[^/]+`
    (matching Snakemake's own default). All other characters in the pattern are
    escaped for literal regex matching.
    """
    parts: list[str] = []
    cursor = 0
    for match in _WILDCARD_RE.finditer(pattern):
        literal = pattern[cursor : match.start()]
        parts.append(re.escape(literal))
        name = match.group(1)
        body = constraints.get(name, r"[^/]+")
        parts.append(f"(?P<{name}>{body})")
        cursor = match.end()
    parts.append(re.escape(pattern[cursor:]))
    return "^" + "".join(parts) + "$"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 7 passed (5 new + 2 fixture smoke tests).

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/patterns.py tests/test_patterns.py
git commit -m "feat(patterns): convert Snakemake wildcard patterns to regex"
```

---

## Task 3: Snakefile resolution

**Files:**
- Modify: `src/snakeprune/patterns.py`
- Modify: `tests/test_patterns.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_patterns.py`:

```python
import pytest
from snakeprune.patterns import resolve_snakefile, SnakefileNotFound


def test_resolve_snakefile_direct(make_pipeline):
    pipeline = make_pipeline("rule all:\n    input: []\n")
    assert resolve_snakefile(pipeline) == pipeline / "Snakefile"


def test_resolve_snakefile_workflow_layout(tmp_path):
    pipeline = tmp_path / "p"
    (pipeline / "workflow").mkdir(parents=True)
    (pipeline / "workflow" / "Snakefile").write_text("rule all:\n    input: []\n")
    assert resolve_snakefile(pipeline) == pipeline / "workflow" / "Snakefile"


def test_resolve_snakefile_missing_raises(tmp_path):
    pipeline = tmp_path / "empty"
    pipeline.mkdir()
    with pytest.raises(SnakefileNotFound) as exc:
        resolve_snakefile(pipeline)
    # Error message must name both checked paths
    msg = str(exc.value)
    assert str(pipeline / "Snakefile") in msg
    assert str(pipeline / "workflow" / "Snakefile") in msg
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 3 failures with `ImportError` for `resolve_snakefile` / `SnakefileNotFound`.

- [ ] **Step 3: Implement Snakefile resolution**

Append to `src/snakeprune/patterns.py`:

```python
from pathlib import Path


class SnakefileNotFound(FileNotFoundError):
    """Raised when no Snakefile exists at either standard location."""


def resolve_snakefile(pipeline_dir: Path) -> Path:
    """Find the Snakefile in `pipeline_dir`.

    Checks `<pipeline_dir>/Snakefile` first, then `<pipeline_dir>/workflow/Snakefile`
    (Snakemake's recommended layout). Raises `SnakefileNotFound` if neither exists.
    """
    candidates = [pipeline_dir / "Snakefile", pipeline_dir / "workflow" / "Snakefile"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SnakefileNotFound(
        "No Snakefile found at either:\n"
        f"  {candidates[0]}\n"
        f"  {candidates[1]}\n"
        "Pass --pipeline-dir pointing at the directory containing your Snakefile."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/patterns.py tests/test_patterns.py
git commit -m "feat(patterns): resolve Snakefile location"
```

---

## Task 4: Load workflow and extract raw rule outputs

**Files:**
- Modify: `src/snakeprune/patterns.py`
- Modify: `tests/test_patterns.py`

This task uses the Snakemake Python API. The exact API surface in Snakemake 8 is `snakemake.api.SnakemakeApi`. If the API doesn't behave as documented below during implementation, iterate by checking `snakemake.api.__doc__` and the Snakemake source for the installed version — the goal is to get a list of rules with `.name`, `.output`, and the workflow's effective wildcard constraints.

- [ ] **Step 1: Write a failing test for raw rule extraction**

Append to `tests/test_patterns.py`:

```python
from snakeprune.patterns import load_rule_specs, RuleSpec


def test_load_rule_specs_single_rule(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    assert len(specs) == 1
    assert specs[0].name == "a"
    assert specs[0].outputs == ["results/{x}.txt"]


def test_load_rule_specs_global_constraints_visible(make_pipeline):
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    x = r'[0-9]+'\n"
        "\n"
        "rule a:\n"
        "    output: 'results/{x}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    assert specs[0].constraints.get("x") == "[0-9]+"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 2 failures with `ImportError` for `load_rule_specs` / `RuleSpec`.

- [ ] **Step 3: Implement `load_rule_specs`**

Append to `src/snakeprune/patterns.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RuleSpec:
    """Raw rule output information extracted from a loaded workflow."""
    name: str
    outputs: list[str]
    constraints: dict[str, str]


def load_rule_specs(pipeline_dir: Path) -> list[RuleSpec]:
    """Load the Snakemake workflow at `pipeline_dir` and extract per-rule output specs.

    Each rule contributes a RuleSpec with its name, the raw output pattern strings,
    and the effective wildcard_constraints (rule-local overriding workflow-global).
    """
    snakefile = resolve_snakefile(pipeline_dir)

    # Snakemake API import is local so the package can be imported without snakemake
    # being installed (unlikely in practice, but keeps test failures cleaner if so).
    from snakemake.api import SnakemakeApi
    from snakemake.settings.types import (
        ConfigSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    with SnakemakeApi(OutputSettings(quiet={"all"})) as api:
        workflow_api = api.workflow(
            resource_settings=ResourceSettings(),
            config_settings=ConfigSettings(),
            storage_settings=StorageSettings(),
            workflow_settings=WorkflowSettings(),
            snakefile=snakefile,
        )
        # Force rule resolution by accessing the underlying workflow.
        workflow = workflow_api._workflow
        global_constraints = dict(getattr(workflow, "_wildcard_constraints", {}) or {})

        specs: list[RuleSpec] = []
        for rule in workflow.rules:
            outputs = [str(o) for o in rule.output]
            rule_constraints = dict(getattr(rule, "wildcard_constraints", {}) or {})
            # Merge: rule-local overrides workflow-global.
            effective = {**global_constraints, **rule_constraints}
            specs.append(RuleSpec(name=rule.name, outputs=outputs, constraints=effective))

    return specs
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -20
```

Expected: 12 passed.

If failures occur because of Snakemake API differences:
- Check `python -c "from snakemake.api import SnakemakeApi; help(SnakemakeApi)"` for the correct signature
- The underlying `Workflow` object may be at `workflow_api.workflow` or `workflow_api._workflow` depending on version; try both
- Wildcard constraints may live at `workflow.wildcard_constraints` (no leading underscore) or `workflow._wildcard_constraints`

Iterate the implementation until both tests pass. Do not skip these tests.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/patterns.py tests/test_patterns.py
git commit -m "feat(patterns): load rule specs from Snakemake workflow"
```

---

## Task 5: Build the compiled-pattern list

**Files:**
- Modify: `src/snakeprune/patterns.py`
- Modify: `tests/test_patterns.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_patterns.py`:

```python
import re
from snakeprune.patterns import find_rule_patterns


def test_find_rule_patterns_compiles_with_constraints(make_pipeline):
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    n = r'\\d+'\n"
        "\n"
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    patterns = find_rule_patterns(pipeline)
    assert len(patterns) == 1
    name, regex = patterns[0]
    assert name == "a"
    assert regex.match("results/123.txt")
    assert not regex.match("results/abc.txt")


def test_find_rule_patterns_multiext_expands(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: multiext('results/{n}', '.txt', '.csv')\n"
        "    shell: 'touch {output}'\n"
    )
    patterns = find_rule_patterns(pipeline)
    # multiext should expand to two distinct patterns
    assert len(patterns) == 2
    matched_extensions = set()
    for _, regex in patterns:
        m = regex.match("results/123.txt") or regex.match("results/123.csv")
        if m:
            matched_extensions.add("ok")
    assert "ok" in matched_extensions
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 2 failures with `ImportError` for `find_rule_patterns`.

- [ ] **Step 3: Implement `find_rule_patterns`**

Append to `src/snakeprune/patterns.py`:

```python
def find_rule_patterns(pipeline_dir: Path) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per output pattern.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern.
    """
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(output_str, spec.constraints)
            out.append((spec.name, re.compile(regex_str)))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_patterns.py -v 2>&1 | tail -10
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/patterns.py tests/test_patterns.py
git commit -m "feat(patterns): build compiled rule-pattern list"
```

---

## Task 6: File enumeration

**Files:**
- Create: `src/snakeprune/walker.py`
- Create: `tests/test_walker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walker.py`:

```python
from snakeprune.walker import iter_results_files


def test_iter_results_files_basic(make_results):
    results = make_results(["a.txt", "sub/b.txt", "sub/deeper/c.csv"])
    paths = sorted(p.relative_to(results).as_posix() for p in iter_results_files(results))
    assert paths == ["a.txt", "sub/b.txt", "sub/deeper/c.csv"]


def test_iter_results_files_skips_symlinks_by_default(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    paths = [p.name for p in iter_results_files(results)]
    assert "a.txt" in paths
    assert "link.txt" not in paths


def test_iter_results_files_follow_symlinks_when_requested(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    paths = [p.name for p in iter_results_files(results, follow_symlinks=True)]
    assert "link.txt" in paths


def test_iter_results_files_ignore_globs(make_results):
    results = make_results(["a.txt", "notes/manual.md", "x.log"])
    paths = sorted(
        p.relative_to(results).as_posix()
        for p in iter_results_files(results, ignore_globs=["notes/**", "*.log"])
    )
    assert paths == ["a.txt"]


def test_iter_results_files_directories_not_returned(make_results):
    results = make_results(["sub/a.txt"])
    for p in iter_results_files(results):
        assert p.is_file()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 5 failures with `ImportError` for `iter_results_files`.

- [ ] **Step 3: Implement `iter_results_files`**

Create `src/snakeprune/walker.py`:

```python
"""Walk a results directory and apply ignore/symlink filters."""
from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Iterator


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    return any(fnmatchcase(rel_path, g) or _glob_matches_subdir(rel_path, g) for g in globs)


def _glob_matches_subdir(rel_path: str, glob: str) -> bool:
    # Treat 'sub/**' as matching anything under 'sub/'
    if glob.endswith("/**"):
        prefix = glob[: -len("/**")]
        return rel_path == prefix or rel_path.startswith(prefix + "/")
    return False


def iter_results_files(
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Yield regular files under `results_dir`, skipping ignored paths and (by default) symlinks."""
    ignore_globs = tuple(ignore_globs)
    for path in results_dir.rglob("*"):
        if path.is_symlink() and not follow_symlinks:
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(results_dir).as_posix()
        if ignore_globs and _matches_any_glob(rel, ignore_globs):
            continue
        yield path
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "feat(walker): enumerate results files with ignore globs"
```

---

## Task 7: Orphan detection

**Files:**
- Modify: `src/snakeprune/walker.py`
- Modify: `tests/test_walker.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_walker.py`:

```python
from snakeprune.walker import find_orphans, OrphanFile


def test_find_orphans_distinguishes_live_and_orphan(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "2.txt", "obsolete/3.txt"])
    orphans = find_orphans(pipeline, results)
    rel = sorted(o.path.relative_to(results).as_posix() for o in orphans)
    assert rel == ["obsolete/3.txt"]


def test_find_orphans_empty_results(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results([])
    assert find_orphans(pipeline, results) == []


def test_find_orphans_ignore_globs_excluded_from_orphan_set(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "notes/manual.md"])
    orphans = find_orphans(pipeline, results, ignore_globs=["notes/**"])
    assert orphans == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 3 failures.

- [ ] **Step 3: Implement `find_orphans`**

Append to `src/snakeprune/walker.py`:

```python
from dataclasses import dataclass
import re

from snakeprune.patterns import find_rule_patterns


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    likely_rule: str | None = None


def find_orphans(
    pipeline_dir: Path,
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
) -> list[OrphanFile]:
    """Return regular files under `results_dir` that match no rule output pattern."""
    patterns = find_rule_patterns(pipeline_dir)
    orphans: list[OrphanFile] = []
    for path in iter_results_files(results_dir, ignore_globs=ignore_globs, follow_symlinks=follow_symlinks):
        rel = path.relative_to(results_dir.parent).as_posix() if results_dir.parent != Path('.') else path.as_posix()
        # Rule output patterns are written relative to the project root (e.g. "results/..."),
        # so match against `results_dir.name + "/" + relative path from results_dir`.
        match_target = (results_dir.name + "/" + path.relative_to(results_dir).as_posix())
        if not any(p.match(match_target) for _, p in patterns):
            orphans.append(OrphanFile(path=path))
    return orphans
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "feat(walker): detect orphan files against rule patterns"
```

---

## Task 8: Rule attribution for orphans

**Files:**
- Modify: `src/snakeprune/walker.py`
- Modify: `tests/test_walker.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_walker.py`:

```python
def test_find_orphans_with_attribution_guesses_closest_rule(make_pipeline, make_results):
    # Rule pattern uses two wildcards; orphan file has the right directory structure
    # but extra path components — should still be attributed to this rule as the
    # closest match by directory prefix.
    pipeline = make_pipeline(
        "rule egene_model:\n"
        "    output: 'results/exp_models/{panel}/{ensid}.csv'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["exp_models/1kGP/extra_subdir/ENSG001.csv"])
    orphans = find_orphans(pipeline, results, attribute_rules=True)
    assert len(orphans) == 1
    assert orphans[0].likely_rule == "egene_model"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 1 failure (`attribute_rules` parameter unrecognised, or `likely_rule` is None).

- [ ] **Step 3: Implement attribution**

Modify `find_orphans` in `src/snakeprune/walker.py` to accept `attribute_rules: bool = False` and add the helper:

```python
def _attribute_rule(target: str, patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """Best-effort guess: the rule whose output pattern shares the longest literal
    prefix with `target`. Falls back to None if no rule shares a meaningful prefix.
    """
    best_rule: str | None = None
    best_prefix_len = 0
    for name, regex in patterns:
        # Reconstruct the literal prefix by reading regex.pattern up to the first '('
        # (the first wildcard capture group). Anchored '^' is the first character.
        literal_prefix = ""
        body = regex.pattern.lstrip("^")
        for ch in body:
            if ch == "(":
                break
            literal_prefix += ch
        # Un-escape: in `re.escape`, '/' is left as-is, so the prefix is a real path prefix
        # except for '\\' before special characters. Strip backslashes that precede
        # ASCII non-alphanumeric chars to recover the source path.
        unescaped = re.sub(r"\\(.)", r"\1", literal_prefix)
        if target.startswith(unescaped) and len(unescaped) > best_prefix_len:
            best_prefix_len = len(unescaped)
            best_rule = name
    return best_rule
```

Then update `find_orphans` to use it when `attribute_rules=True`:

```python
def find_orphans(
    pipeline_dir: Path,
    results_dir: Path,
    ignore_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
    attribute_rules: bool = False,
) -> list[OrphanFile]:
    patterns = find_rule_patterns(pipeline_dir)
    orphans: list[OrphanFile] = []
    for path in iter_results_files(results_dir, ignore_globs=ignore_globs, follow_symlinks=follow_symlinks):
        match_target = results_dir.name + "/" + path.relative_to(results_dir).as_posix()
        if any(p.match(match_target) for _, p in patterns):
            continue
        likely = _attribute_rule(match_target, patterns) if attribute_rules else None
        orphans.append(OrphanFile(path=path, likely_rule=likely))
    return orphans
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_walker.py -v 2>&1 | tail -10
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "feat(walker): attribute each orphan to its closest rule"
```

---

## Task 9: Safe deletion

**Files:**
- Create: `src/snakeprune/delete.py`
- Create: `tests/test_delete.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_delete.py`:

```python
import pytest
from pathlib import Path

from snakeprune.delete import delete_orphans
from snakeprune.walker import OrphanFile


def test_delete_orphans_unlinks_regular_files(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data")
    delete_orphans([OrphanFile(path=f)], allow_symlinks=False)
    assert not f.exists()


def test_delete_orphans_refuses_symlink_by_default(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        delete_orphans([OrphanFile(path=link)], allow_symlinks=False)
    # Both still exist
    assert target.exists()
    assert link.is_symlink()


def test_delete_orphans_allows_symlink_with_flag(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    delete_orphans([OrphanFile(path=link)], allow_symlinks=True)
    assert not link.is_symlink()
    # Target untouched (we only unlinked the symlink itself)
    assert target.exists()


def test_delete_orphans_refuses_directories(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        delete_orphans([OrphanFile(path=d)], allow_symlinks=False)
    assert d.is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_delete.py -v 2>&1 | tail -10
```

Expected: 4 failures with `ImportError` for `delete_orphans`.

- [ ] **Step 3: Implement `delete_orphans`**

Create `src/snakeprune/delete.py`:

```python
"""Safe deletion of orphan files."""
from __future__ import annotations

import sys
from typing import Iterable

from snakeprune.walker import OrphanFile


def delete_orphans(orphans: Iterable[OrphanFile], allow_symlinks: bool = False) -> None:
    """Unlink each orphan file. Refuses to operate on directories. Refuses symlinks
    unless `allow_symlinks=True`. Prints what's being deleted to stderr.
    """
    for orphan in orphans:
        path = orphan.path
        if path.is_dir() and not path.is_symlink():
            raise IsADirectoryError(f"Refusing to delete directory: {path}")
        if path.is_symlink() and not allow_symlinks:
            raise PermissionError(
                f"Refusing to delete symlink {path} without --allow-symlinks"
            )
        print(f"deleting: {path}", file=sys.stderr)
        path.unlink()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_delete.py -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/delete.py tests/test_delete.py
git commit -m "feat(delete): safe unlink of orphan regular files"
```

---

## Task 10: Typer CLI

**Files:**
- Create: `src/snakeprune/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
from typer.testing import CliRunner

from snakeprune.cli import app

runner = CliRunner()


def test_cli_scan_reports_orphans(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    assert "obsolete.csv" in result.stdout
    # Live files not listed by default
    assert "1.txt" not in result.stdout


def test_cli_scan_dry_run_does_not_delete(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert (results / "obsolete.csv").exists()


def test_cli_scan_delete_flag_unlinks(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--delete"])
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()


def test_cli_scan_no_snakefile_fails_cleanly(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    result = runner.invoke(app, ["scan", str(empty), str(results)])
    assert result.exit_code != 0
    assert "Snakefile" in result.stdout or "Snakefile" in (result.stderr or "")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cli.py -v 2>&1 | tail -10
```

Expected: 4 failures with `ImportError` for `snakeprune.cli`.

- [ ] **Step 3: Implement the CLI**

Create `src/snakeprune/cli.py`:

```python
"""snakeprune CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from snakeprune.patterns import SnakefileNotFound
from snakeprune.walker import find_orphans
from snakeprune.delete import delete_orphans

app = typer.Typer(add_completion=False, help="Find orphan files in a Snakemake results tree.")


@app.command()
def scan(
    pipeline_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    results_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    delete: bool = typer.Option(False, "--delete", help="Unlink orphans (default: dry-run only)."),
    rule_attribution: bool = typer.Option(False, "--rule-attribution", help="Show best-guess rule per orphan."),
    ignore: Optional[list[str]] = typer.Option(None, "--ignore", help="Glob pattern to skip; repeatable."),
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks", help="Follow symlinks (default: skip)."),
    allow_symlinks: bool = typer.Option(False, "--allow-symlinks", help="Allow deleting symlinks when --delete is set."),
) -> None:
    """Scan a Snakemake project's results directory for orphan files."""
    try:
        orphans = find_orphans(
            pipeline_dir=pipeline_dir,
            results_dir=results_dir,
            ignore_globs=tuple(ignore or ()),
            follow_symlinks=follow_symlinks,
            attribute_rules=rule_attribution,
        )
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    for orphan in orphans:
        line = str(orphan.path)
        if rule_attribution and orphan.likely_rule:
            line += f"\t(likely: {orphan.likely_rule})"
        typer.echo(line)

    if delete and orphans:
        delete_orphans(orphans, allow_symlinks=allow_symlinks)
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): scan subcommand wiring everything together"
```

---

## Task 11: End-to-end smoke test on real pipeline

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Manually run snakeprune against the genex pipeline**

```bash
cd ~/projects/snakeprune
snakeprune scan ~/projects/genex/smk ~/projects/genex/smk/results 2>&1 | head -30
```

Expected: a non-zero number of orphan paths printed, no exceptions.

- [ ] **Step 2: Verify a known orphan is detected**

Pick a known-obsolete file pattern (e.g., paths missing the recently-added `{pc_orth}` or `{pc_transform}` wildcard segments), confirm it appears in snakeprune's output.

- [ ] **Step 3: Update README with usage**

Append to `README.md`:

````markdown
## Usage

```bash
# Dry-run: list orphans, take no action
snakeprune scan path/to/pipeline path/to/results

# With rule attribution
snakeprune scan path/to/pipeline path/to/results --rule-attribution

# Skip intentional manual files
snakeprune scan path/to/pipeline path/to/results --ignore "notes/**" --ignore "*.log"

# Actually delete (refuses symlinks unless --allow-symlinks)
snakeprune scan path/to/pipeline path/to/results --delete
```
````

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add usage examples"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|--------------|------|
| Snakefile resolution (`Snakefile` then `workflow/Snakefile`) | Task 3 |
| Pattern extraction via Snakemake API | Task 4 |
| Wildcard → regex with rule-local + global constraints, default `[^/]+` | Task 2 |
| `multiext` expansion | Task 5 (via API natural expansion) |
| Compiled pattern list | Task 5 |
| File enumeration with ignore globs | Task 6 |
| Skip symlinks by default, follow with flag | Task 6 |
| Orphan detection | Task 7 |
| Rule attribution | Task 8 |
| Safe deletion (regular files only, symlink refusal) | Task 9 |
| Typer CLI with all documented flags | Task 10 |
| Missing-Snakefile error message names both checked paths | Task 3 + Task 10 |

**Placeholder scan:** None. Every code step has complete code.

**Type consistency:**
- `OrphanFile` defined in Task 7, used in Task 8 (with `likely_rule` added), used in Task 9 (deletion), used in Task 10 (CLI output). Consistent.
- `find_orphans` signature evolves from Task 7 (no attribution) → Task 8 (adds `attribute_rules=False`). Tests in Task 7 don't pass that arg, so backward-compatible.
- `RuleSpec` defined in Task 4, consumed only in Task 5. Consistent.
- `wildcard_pattern_to_regex(pattern, constraints)` defined in Task 2, called the same way in Task 5. Consistent.
