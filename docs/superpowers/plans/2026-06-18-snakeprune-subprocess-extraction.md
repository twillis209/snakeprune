# snakeprune Subprocess-Based Rule Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move rule extraction out of snakeprune's interpreter into a subprocess so snakeprune itself becomes pure-stdlib, and add `--configfile` passthrough so config-conditional rules can be resolved.

**Architecture:** A new standalone script `src/snakeprune/_extract.py` does what `patterns.load_rule_specs` does today: load the workflow via `SnakemakeApi`, walk `workflow.rules`, emit a `{"rules": [...]}` JSON document to stdout. `patterns.run_extractor` invokes that script via `subprocess.run` using whatever `python` is on `$PATH` (the user's activated workflow env). `load_rule_specs` becomes a thin wrapper so existing callers and tests keep working. `snakemake` moves out of snakeprune's runtime deps into a `dev` extra.

**Tech Stack:** Python 3.12+, stdlib `subprocess` + `json` + `shutil`, `snakemake` (dev-only after Task 5), Typer, pytest.

## Global Constraints

- Python type hints throughout (`from __future__ import annotations` already standard).
- TDD: every behaviour change starts with a failing test.
- New tests go in existing `tests/test_<module>.py` files; no new test files.
- Each task ends with one commit and one `git push` to `origin/main` (standing auto-push permission).
- Exit code conventions: 0 = success / clean abort, 2 = pre-existing usage errors, 3 = safety refusals, 4 = environment / subprocess problems (new in this plan).
- `_extract.py` is standalone: no imports from `snakeprune`, only stdlib + `snakemake`. It is shipped as a `.py` file inside the package and located at runtime via `Path(__file__).parent / "_extract.py"`.
- IPC contract: extractor emits `{"rules": [{"name": str, "outputs": list[str], "constraints": dict[str, str]}, ...]}` to stdout. Stderr is reserved for error messages.
- `--python` and `--config KEY=VALUE` CLI flags are **explicitly out of scope** (rejected during brainstorming).
- `patterns.load_rule_specs(pipeline_dir, configfiles=())` must remain a working public function so existing third-party callers and the test suite don't break.

---

### Task 1: Standalone `_extract.py` extractor script

**Files:**
- Create: `src/snakeprune/_extract.py`
- Modify: `tests/test_patterns.py` (add subprocess-level tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: a CLI-style script invokable as `python /path/to/_extract.py <pipeline_dir> [--configfile PATH ...]`. On success: exit 0, JSON document `{"rules": [...]}` on stdout. On error: exit nonzero, error message on stderr.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_patterns.py`:

```python
import json as _json
import subprocess as _subprocess
import sys as _sys
from pathlib import Path as _Path


def _extract_script() -> _Path:
    """Resolve the absolute path of src/snakeprune/_extract.py from the test file."""
    return _Path(__file__).resolve().parent.parent / "src" / "snakeprune" / "_extract.py"


def test_extract_script_emits_rules_json(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    result = _subprocess.run(
        [_sys.executable, str(_extract_script()), str(pipeline)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = _json.loads(result.stdout)
    assert payload["rules"][0]["name"] == "a"
    assert payload["rules"][0]["outputs"] == ["results/{n}.txt"]


def test_extract_script_strips_inline_constraints(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    result = _subprocess.run(
        [_sys.executable, str(_extract_script()), str(pipeline)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rule = _json.loads(result.stdout)["rules"][0]
    assert rule["outputs"] == ["results/{x}.txt"]
    assert rule["constraints"].get("x") == "[0-9]+"


def test_extract_script_missing_snakefile_exits_nonzero(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _subprocess.run(
        [_sys.executable, str(_extract_script()), str(empty)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Snakefile" in result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_patterns.py -k "extract_script" -v
```

Expected: FAIL with `FileNotFoundError` or similar — `_extract.py` does not exist.

- [ ] **Step 3: Implement `_extract.py`**

Create `src/snakeprune/_extract.py`:

```python
"""Standalone rule extractor for snakeprune.

This script is invoked as a subprocess by snakeprune's CLI:
    python _extract.py <pipeline_dir> [--configfile PATH ...]

It loads the Snakemake workflow at <pipeline_dir>, iterates over each rule's
declared outputs, strips Snakemake's inline `{name,regex}` constraint
annotations, merges effective wildcard constraints, and emits the result
as a JSON document on stdout:

    {"rules": [{"name": str, "outputs": list[str], "constraints": dict[str, str]}, ...]}

Errors go to stderr; the script exits nonzero on any failure.

Standalone: this file must not import anything from `snakeprune`. snakeprune
the CLI ships it alongside `patterns.py` but does not import from it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_WILDCARD_NAME_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")
_INLINE_CONSTRAINT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*),[^{}]*\}")


def _strip_inline_constraints(pattern: str) -> tuple[str, dict[str, str]]:
    constraints: dict[str, str] = {}

    def _record(match: re.Match[str]) -> str:
        name = match.group(1)
        body = match.group(0)[len(name) + 2 : -1]
        constraints[name] = body
        return "{" + name + "}"

    stripped = _INLINE_CONSTRAINT_RE.sub(_record, pattern)
    return stripped, constraints


def _resolve_snakefile(pipeline_dir: Path) -> Path:
    candidates = [pipeline_dir / "Snakefile", pipeline_dir / "workflow" / "Snakefile"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No Snakefile found at either:\n"
        f"  {candidates[0]}\n"
        f"  {candidates[1]}"
    )


def _load_workflow(snakefile: Path, workdir: Path, configfiles: list[Path]):
    from snakemake.api import SnakemakeApi
    from snakemake.settings.enums import Quietness
    from snakemake.settings.types import (
        ConfigSettings,
        OutputSettings,
        ResourceSettings,
        StorageSettings,
        WorkflowSettings,
    )

    api = SnakemakeApi(OutputSettings(quiet={Quietness.ALL}))
    workflow_api = api.workflow(
        resource_settings=ResourceSettings(),
        config_settings=ConfigSettings(configfiles=configfiles),
        storage_settings=StorageSettings(),
        workflow_settings=WorkflowSettings(),
        snakefile=snakefile,
        workdir=workdir,
    )
    return api, workflow_api._workflow


def extract(pipeline_dir: Path, configfiles: list[Path]) -> dict:
    snakefile = _resolve_snakefile(pipeline_dir)
    api, workflow = _load_workflow(
        snakefile=snakefile,
        workdir=pipeline_dir.resolve(),
        configfiles=configfiles,
    )
    try:
        global_constraints = dict(
            getattr(workflow, "wildcard_constraints", {}) or {}
        )
        rules_out: list[dict] = []
        for rule in workflow.rules:
            raw_outputs: list[str] = []
            inline_constraints: dict[str, str] = {}
            for o in rule.output:
                stripped, inline = _strip_inline_constraints(str(o))
                raw_outputs.append(stripped)
                inline_constraints.update(inline)
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            effective = {
                **global_constraints,
                **inline_constraints,
                **rule_constraints,
            }
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "constraints": effective,
                }
            )
        return {"rules": rules_out}
    finally:
        api.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Snakemake rule outputs as JSON.",
    )
    parser.add_argument("pipeline_dir", type=Path)
    parser.add_argument(
        "--configfile",
        type=Path,
        action="append",
        default=[],
        help="Snakemake configfile; repeatable.",
    )
    args = parser.parse_args(argv)

    try:
        result = extract(args.pipeline_dir, args.configfile)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"extractor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_patterns.py -k "extract_script" -v
```

Expected: all PASS.

- [ ] **Step 5: Run the full suite (no regressions)**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 6: Commit and push**

```
git add src/snakeprune/_extract.py tests/test_patterns.py
git commit -m "feat(extract): standalone Snakemake rule extractor script"
git push
```

---

### Task 2: `run_extractor` + `load_rule_specs` wrapper

**Files:**
- Modify: `src/snakeprune/patterns.py`
- Existing tests in `tests/test_patterns.py` continue to exercise `load_rule_specs`; no new tests in this task — Task 1's subprocess tests already cover the extractor, and the existing `load_rule_specs_*` tests now exercise the wrapper end-to-end.

**Interfaces:**
- Consumes: Task 1's `_extract.py` as a runnable script.
- Produces:
  - New class `patterns.ExtractorError(RuntimeError)` — raised when the extractor subprocess fails or produces bad output. Carries the user-facing message in `str(exc)`.
  - New function `patterns.run_extractor(pipeline_dir: Path, configfiles: Sequence[Path] = (), *, _python_exe_for_testing: Path | None = None, _script_path_for_testing: Path | None = None) -> list[RuleSpec]`. The two `_*_for_testing` kwargs are private test seams (documented in the docstring; not in the public help).
  - `patterns.load_rule_specs(pipeline_dir: Path, configfiles: Sequence[Path] = ()) -> list[RuleSpec]` — preserved signature, now delegates to `run_extractor`. Existing callers see no change.
  - `find_rule_patterns(pipeline_dir: Path, configfiles: Sequence[Path] = ()) -> list[tuple[str, re.Pattern]]` — gains `configfiles` kwarg, passes it through.
  - Removes: the in-process body of `load_rule_specs` (the `with SnakemakeApi(...) as api: ...` block) and the now-unused `_strip_inline_constraints` and its regex (kept only in `_extract.py`).

- [ ] **Step 1: Replace `patterns.load_rule_specs` body and add `run_extractor`**

Edit `src/snakeprune/patterns.py`. At the top of the file (under existing imports), add:

```python
import json
import shutil
import subprocess
from typing import Sequence
```

Remove the existing `_INLINE_CONSTRAINT_RE` definition and the `_strip_inline_constraints` function (they have moved to `_extract.py`).

Add a new exception class above `load_rule_specs`:

```python
class ExtractorError(RuntimeError):
    """Raised when the rule-extractor subprocess fails or produces invalid output.

    The user-facing message is ``str(exc)``; the CLI translates this into
    exit code 4 with the message on stderr.
    """
```

Add a helper that resolves the script path:

```python
def _extract_script_path() -> Path:
    """Resolve the absolute path of the `_extract.py` script shipped with this package."""
    return Path(__file__).parent / "_extract.py"
```

Replace the body of `load_rule_specs` with a delegation, and add `run_extractor`:

```python
def load_rule_specs(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
) -> list[RuleSpec]:
    """Load rule output specs by running the standalone extractor in a subprocess.

    The signature is preserved from the in-process implementation so existing
    third-party callers and the project's own test suite keep working.
    """
    return run_extractor(pipeline_dir, configfiles=configfiles)


def run_extractor(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    *,
    _python_exe_for_testing: Path | None = None,
    _script_path_for_testing: Path | None = None,
) -> list[RuleSpec]:
    """Invoke the standalone extractor in a subprocess and return RuleSpec objects.

    The two `_*_for_testing` kwargs are private test seams. They override
    the `python` discovery and the script-path resolution respectively, so
    error-path tests can substitute a stub script or a deliberately-missing
    interpreter without touching the real environment.
    """
    if _python_exe_for_testing is not None:
        python_exe = Path(_python_exe_for_testing)
    else:
        found = shutil.which("python")
        if found is None:
            raise ExtractorError(
                "Python interpreter `python` not found on PATH. Activate "
                "your workflow environment where you would normally run "
                "`snakemake`."
            )
        python_exe = Path(found)

    script_path = (
        Path(_script_path_for_testing)
        if _script_path_for_testing is not None
        else _extract_script_path()
    )

    cmd: list[str] = [str(python_exe), str(script_path), str(pipeline_dir)]
    for cf in configfiles:
        cmd.extend(["--configfile", str(cf)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise ExtractorError(
            f"Extractor failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractorError(
            "Extractor produced unparseable output. This is a bug; please "
            f"report. stderr was:\n{result.stderr.strip()}"
        ) from exc

    return [
        RuleSpec(
            name=r["name"],
            outputs=list(r["outputs"]),
            constraints=dict(r["constraints"]),
        )
        for r in payload["rules"]
    ]
```

Update `find_rule_patterns`:

```python
def find_rule_patterns(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per output pattern."""
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(output_str, spec.constraints)
            out.append((spec.name, re.compile(regex_str)))
    return out
```

- [ ] **Step 2: Run the full suite — existing `load_rule_specs_*` tests now exercise the subprocess flow**

```
python -m pytest -q
```

Expected: all PASS. The existing `test_load_rule_specs_*` tests in `tests/test_patterns.py` exercise `load_rule_specs(pipeline)` and assert the same RuleSpec contents — those assertions now travel through the subprocess but the output should match.

If any test fails with a `KeyError` on `outputs`/`constraints`/`name` in the JSON, inspect the failing payload: the JSON shape must match the spec exactly.

- [ ] **Step 3: Commit and push**

```
git add src/snakeprune/patterns.py
git commit -m "refactor(patterns): run_extractor subprocess + load_rule_specs wrapper"
git push
```

---

### Task 3: `--configfile` CLI flag

**Files:**
- Modify: `src/snakeprune/cli.py`
- Modify: `tests/test_cli.py` (one new test)

**Interfaces:**
- Consumes: `find_rule_patterns(pipeline_dir, configfiles=...)` from Task 2.
- Produces: new repeatable `--configfile PATH` option on `scan` that forwards to `find_rule_patterns`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_cli_scan_forwards_configfile_to_extractor(make_pipeline, tmp_path):
    # Rule is gated by config["do_qc"] which defaults to False. Without
    # --configfile, the rule is absent and its expected output file looks
    # like an orphan. With --configfile do_qc.yaml (sets do_qc: true),
    # the rule is present and the output is recognised as live.
    pipeline = make_pipeline(
        "if config.get('do_qc', False):\n"
        "    rule qc:\n"
        "        output: 'results/qc/{sample}.tsv'\n"
        "        shell: 'touch {output}'\n"
        "\n"
        "rule align:\n"
        "    output: 'results/align/{sample}.bam'\n"
        "    shell: 'touch {output}'\n"
    )
    configfile = pipeline / "do_qc.yaml"
    configfile.write_text("do_qc: true\n")
    results = pipeline.parent / "results"
    results.mkdir()
    (results / "qc").mkdir()
    (results / "qc" / "s1.tsv").touch()
    (results / "align").mkdir()
    (results / "align" / "s1.bam").touch()

    # Without configfile: qc rule is hidden, qc output flagged as orphan.
    result_no_cfg = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--allow-high-orphan-rate"]
    )
    assert result_no_cfg.exit_code == 0
    assert "qc/s1.tsv" in result_no_cfg.stdout

    # With configfile: qc rule visible, output recognised as live.
    result_with_cfg = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--configfile", str(configfile)]
    )
    assert result_with_cfg.exit_code == 0
    assert "qc/s1.tsv" not in result_with_cfg.stdout
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_cli.py::test_cli_scan_forwards_configfile_to_extractor -v
```

Expected: FAIL with `no such option: --configfile`.

- [ ] **Step 3: Add the flag in `cli.py` and thread it through**

Edit `src/snakeprune/cli.py`. Add the option in the `scan` signature, after `trash` (which is the last new flag from the safety plan):

```python
    configfile: Optional[list[Path]] = typer.Option(
        None,
        "--configfile",
        help="Snakemake configfile to pass to the extractor; repeatable.",
    ),
```

Update the `find_rule_patterns(...)` call near the top of `scan`:

```python
        patterns = find_rule_patterns(
            pipeline_dir, configfiles=tuple(configfile or ())
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```
python -m pytest tests/test_cli.py::test_cli_scan_forwards_configfile_to_extractor -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 6: Commit and push**

```
git add src/snakeprune/cli.py tests/test_cli.py
git commit -m "feat(cli): --configfile passthrough resolves config-gated rules"
git push
```

---

### Task 4: Error UX — specific failure messages

**Files:**
- Modify: `src/snakeprune/cli.py` (catch `ExtractorError`, exit 4)
- Modify: `src/snakeprune/patterns.py` (specific branch for "snakemake not importable")
- Modify: `tests/test_patterns.py` and `tests/test_cli.py` (failure-path tests)

**Interfaces:**
- Consumes: `run_extractor` and `ExtractorError` from Task 2.
- Produces: a clear translation table from each failure mode to a user-facing stderr message + exit code 4. No new public symbols beyond what Task 2 added.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_patterns.py`:

```python
from snakeprune.patterns import ExtractorError, run_extractor


def test_run_extractor_python_not_on_path_message(monkeypatch, make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    monkeypatch.setattr("snakeprune.patterns.shutil.which", lambda _name: None)
    with pytest.raises(ExtractorError) as exc:
        run_extractor(pipeline)
    msg = str(exc.value)
    assert "python" in msg.lower()
    assert "PATH" in msg
    assert "snakemake" in msg.lower()


def test_run_extractor_snakemake_missing_message(make_pipeline, tmp_path):
    # Stub script that prints the import-error signature and exits nonzero,
    # simulating a python interpreter without snakemake installed.
    stub = tmp_path / "stub_no_snakemake.py"
    stub.write_text(
        "import sys\n"
        "print(\"ModuleNotFoundError: No module named 'snakemake'\", file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    with pytest.raises(ExtractorError) as exc:
        run_extractor(pipeline, _script_path_for_testing=stub)
    msg = str(exc.value)
    assert "snakemake is not importable" in msg


def test_run_extractor_bad_json_message(make_pipeline, tmp_path):
    stub = tmp_path / "stub_bad_json.py"
    stub.write_text(
        "import sys\n"
        "sys.stdout.write('this is not json\\n')\n"
        "sys.stderr.write('garbage produced for testing\\n')\n"
    )
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    with pytest.raises(ExtractorError) as exc:
        run_extractor(pipeline, _script_path_for_testing=stub)
    msg = str(exc.value)
    assert "unparseable" in msg
    assert "garbage produced for testing" in msg
```

Add to `tests/test_cli.py`:

```python
def test_cli_scan_extractor_failure_exits_4(make_pipeline, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # Force the "python not on PATH" branch via the patterns module.
    monkeypatch.setattr("snakeprune.patterns.shutil.which", lambda _name: None)
    results = pipeline.parent / "results"
    results.mkdir()
    (results / "a.txt").touch()
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 4
    combined = result.stdout + (result.stderr or "")
    assert "PATH" in combined
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_patterns.py -k "run_extractor_" -v
python -m pytest tests/test_cli.py::test_cli_scan_extractor_failure_exits_4 -v
```

Expected: failures — the "snakemake not importable" branch and the "unparseable output" branch don't exist yet in `run_extractor`; the CLI doesn't catch `ExtractorError` yet.

- [ ] **Step 3: Add the specific branch in `run_extractor`**

In `src/snakeprune/patterns.py`, replace the generic-failure block inside `run_extractor` with:

```python
    if result.returncode != 0:
        if "No module named 'snakemake'" in result.stderr:
            raise ExtractorError(
                f"The extractor failed: snakemake is not importable in "
                f"`{python_exe}`. Activate the env where you'd normally run "
                f"`snakemake`."
            )
        raise ExtractorError(
            f"Extractor failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
```

(The bad-JSON branch was already added in Task 2.)

- [ ] **Step 4: Catch `ExtractorError` in the CLI**

In `src/snakeprune/cli.py`, update the imports:

```python
from snakeprune.patterns import (
    ExtractorError,
    SnakefileNotFound,
    combine_rule_patterns,
    extract_literal_prefix,
    find_rule_patterns,
)
```

Wrap the `find_rule_patterns` call in `scan`:

```python
    try:
        patterns = find_rule_patterns(
            pipeline_dir, configfiles=tuple(configfile or ())
        )
    except SnakefileNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    except ExtractorError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=4)
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest -q
```

Expected: all PASS.

- [ ] **Step 6: Commit and push**

```
git add src/snakeprune/patterns.py src/snakeprune/cli.py tests/test_patterns.py tests/test_cli.py
git commit -m "feat(cli): exit 4 with specific messages for extractor failures"
git push
```

---

### Task 5: Move `snakemake` from runtime deps to a `dev` extra in `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: `pip install snakeprune` no longer pulls `snakemake`. `pip install -e ".[dev]"` (or whatever existing dev-extra name the file uses) still pulls it for testing.

- [ ] **Step 1: Read the current `pyproject.toml`**

Look for the `[project] dependencies = [...]` array and any `[project.optional-dependencies]` table.

- [ ] **Step 2: Move `snakemake` to dev/test deps**

Remove `"snakemake>=9.0"` (or whatever exact version pin is currently used) from `[project].dependencies`. If the file already has `[project.optional-dependencies]` with a `dev` or `test` extra, add `"snakemake>=9.0"` there. If not, add:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8",
    "snakemake>=9.0",
]
```

(Use the exact existing version pin for snakemake — don't lower it. If the project already uses a different extra name like `test`, reuse that.)

- [ ] **Step 3: Verify the package install works without `snakemake`**

```
cd /tmp
python -m venv snakeprune-clean
./snakeprune-clean/bin/pip install /Users/twil15/projects/snakeprune
./snakeprune-clean/bin/python -c "from snakeprune.patterns import find_rule_patterns; print('ok')"
./snakeprune-clean/bin/snakeprune --help
```

Expected: install succeeds with no `snakemake` in the venv; the `import` succeeds (because patterns.py no longer top-level-imports snakemake); `--help` works.

```
rm -rf /tmp/snakeprune-clean
```

- [ ] **Step 4: Verify the dev install still works**

In the project's existing dev env:

```
pip install -e ".[dev]"
python -m pytest -q
```

Expected: install succeeds; full suite PASS (snakemake is in the env via the dev extra, so subprocess-based tests still work).

- [ ] **Step 5: Commit and push**

```
git add pyproject.toml
git commit -m "build: snakemake moves from runtime deps to the dev extra"
git push
```

---

### Task 6: README updates

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: all earlier tasks (so the README can describe their behaviour).
- Produces: documentation reflecting the new subprocess flow, the `--configfile` flag, and the loosened install story.

- [ ] **Step 1: Update the `Install` section**

In `README.md`, replace the existing `## Install` section with:

```markdown
## Install

`snakeprune` itself is a small pure-Python CLI. The recommended install is via [`pipx`](https://pipx.pypa.io/) so it lives in its own isolated env and works against any workflow:

```bash
pipx install snakeprune
```

Or, to install from a clone:

```bash
git clone https://github.com/twillis209/snakeprune.git
cd snakeprune
pip install -e .
```

`snakeprune` calls into the workflow's own Python environment via subprocess, so `snakemake` must be importable in *that* environment — but it does not need to live in the same env as `snakeprune`.
```

- [ ] **Step 2: Update the `Runtime requirements` section**

Replace the existing `## Runtime requirements` section with:

```markdown
## Runtime requirements

`snakeprune` invokes a small standalone Python script (`_extract.py`) as a subprocess to load the workflow and emit its rule outputs as JSON. The subprocess is launched with whichever `python` is on `$PATH`, so the user is expected to have activated their workflow's environment before running `snakeprune` (the same env where they would run `snakemake`).

Concretely this means:

- `snakemake` and any Python deps the workflow imports at parse time (`pandas`, `polars`, gene lists, config files) must be importable from the activated env.
- `snakeprune` itself has no `snakemake` runtime dependency, so it can be installed once via `pipx` and reused across many workflows.

If `python` is not on `$PATH`, `snakeprune` exits with code 4 and a message asking the user to activate their workflow env. If `python` is found but `snakemake` is not importable from it, `snakeprune` exits with code 4 and a more specific message pointing at the same fix.
```

- [ ] **Step 3: Update the `Usage` section**

Add a new example after the existing `--trash` example:

```markdown
# Pass config to the workflow loader so config-gated rules are visible
snakeprune scan path/to/pipeline path/to/results --configfile path/to/config.yaml
```

- [ ] **Step 4: Update the `Limitations` subsection of `Safety and limitations`**

Replace the `Config-conditional rules` bullet with:

```markdown
- **Config-conditional rules.** If a rule is `include:`-d only under specific config values, pass the matching config with `--configfile path/to/config.yaml` (repeatable). Without it, the extractor sees an empty config and config-gated rules are absent — their outputs would then be reported as orphans.
```

- [ ] **Step 5: Update the `How it works` section**

Replace steps 1–3 with:

```markdown
1. Resolve the Snakefile (direct, then `workflow/Snakefile` fallback).
2. Locate `python` on `$PATH` and run the bundled `_extract.py` script as a subprocess: `<python> .../_extract.py <pipeline_dir> [--configfile ...]`. The subprocess loads the workflow via `snakemake.api.SnakemakeApi`, walks `workflow.rules`, and emits `{"rules": [...]}` JSON on stdout.
3. For each rule's outputs, substitute `{wildcard}` placeholders with their effective regex bodies (rule-local `wildcard_constraints` override workflow-global; inline `{name,regex}` annotations are honoured; missing constraints default to `[^/]+`). The combined alternation regex is built once for the whole scan.
```

(Steps 4–7 keep their existing text.)

- [ ] **Step 6: Sanity-check the full README**

Open `README.md` and read it top-to-bottom. Confirm the new sections fit cleanly between their neighbours and there are no dangling references to the old in-process behaviour.

- [ ] **Step 7: Run the full suite as a final sanity check**

```
python -m pytest -q
```

Expected: all PASS (no code changes in this task).

- [ ] **Step 8: Commit and push**

```
git add README.md
git commit -m "docs(README): subprocess extraction, --configfile, pipx install"
git push
```

---

## Self-Review

**Spec coverage:**
- Standalone `_extract.py` script → Task 1 ✓
- `patterns.run_extractor` + `load_rule_specs` wrapper + `ExtractorError` → Task 2 ✓
- `find_rule_patterns` accepts `configfiles` → Task 2 ✓
- CLI `--configfile` flag and threading → Task 3 ✓
- Error UX (python missing, snakemake missing, bad JSON) → Task 2 (bad JSON) + Task 4 (python missing, snakemake missing) ✓
- Exit code 4 in CLI on `ExtractorError` → Task 4 ✓
- Private test seams `_python_exe_for_testing` / `_script_path_for_testing` → Task 2 ✓
- `pyproject.toml` snakemake move → Task 5 ✓
- README — Install / Runtime requirements / How it works / Limitations / Usage examples → Task 6 ✓
- Preserved `load_rule_specs` public API → Task 2 ✓

**Placeholder scan:** no TBDs, no "handle appropriately" lines, every code step has the actual code.

**Type consistency:**
- `RuleSpec(name: str, outputs: list[str], constraints: dict[str, str])` — same shape in `_extract.py` JSON output, `run_extractor` reconstruction, and downstream callers.
- `run_extractor(pipeline_dir, configfiles, *, _python_exe_for_testing, _script_path_for_testing)` — same signature in Task 2 implementation and Task 4 tests.
- `ExtractorError(RuntimeError)` — same class in Task 2 definition, Task 4 tests, Task 4 CLI handler.
- `configfiles: Sequence[Path] = ()` — same param name / type in `load_rule_specs`, `run_extractor`, `find_rule_patterns`, and the CLI passthrough.
- CLI `configfile: Optional[list[Path]]` — the singular noun matches the singular flag name `--configfile`; the plural `configfiles=tuple(configfile or ())` matches the patterns API.
