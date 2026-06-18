# snakeprune Subprocess-Based Rule Extraction — Design

**Date:** 2026-06-18
**Status:** Draft, awaiting user review

## Problem

snakeprune currently loads the user's workflow in-process via `snakemake.api.SnakemakeApi`. The workflow is parsed by Python code that runs inside snakeprune's own interpreter, which means every dependency the workflow imports at parse time — `pandas`, `polars`, the user's gene lists and config files, anything else `include:`-d at the top level — must be importable from the same process. In practice this couples snakeprune to whichever environment the user happens to run their pipeline in. They can't `pipx install snakeprune` once and reuse it across projects; they have to install it (and keep it in sync) inside every workflow env they care about.

This also keeps `snakemake` in snakeprune's own runtime dependencies, even though the only thing snakeprune needs from snakemake is "parse this Snakefile and tell me what each rule's outputs are".

A secondary problem is correctness: workflows that gate rules behind config (`if config["do_qc"]: include: "qc.smk"`) silently lose those rules when snakeprune loads them with no config — every output of the missing rule then looks like an orphan. The README's `Safety and limitations` section flags this explicitly today.

## Approach

Split the workflow-parsing step into a tiny standalone Python script (`_extract.py`) that runs as a subprocess inside the user's workflow environment, and have snakeprune the CLI communicate with it through JSON on stdout. snakeprune itself becomes a pure-stdlib tool with no `snakemake` dependency; the extractor script is shipped alongside it but is designed to be executed by a different Python interpreter (the workflow's).

`--configfile PATH` is added as a passthrough so the user can explicitly hand snakemake the same config it would normally see, closing the config-conditional-rules correctness gap as part of the same change.

## Components

### `_extract.py` — standalone extractor

A new file at `src/snakeprune/_extract.py`. Self-contained: no imports from `snakeprune`, only stdlib + `snakemake`. Designed to be executed as `<python> /path/to/_extract.py <pipeline_dir> [--configfile PATH ...]` by a Python interpreter that has `snakemake` installed.

It internally re-implements what today's `patterns.load_rule_specs` does:

- Resolve `<pipeline_dir>/Snakefile`, falling back to `<pipeline_dir>/workflow/Snakefile`. Exit nonzero with a clear stderr message if neither exists.
- Strip inline `{name,regex}` constraint annotations from each rule's outputs.
- Merge effective wildcard constraints in precedence workflow-global < inline < rule-local.
- Iterate `workflow.rules` and build a list of `{"name": str, "outputs": list[str], "constraints": dict[str, str]}` dicts.
- Emit `{"rules": [...]}` as JSON to stdout. Stderr is reserved for errors.

The two helpers needed by the extractor (`_strip_inline_constraints` and a snakefile-resolution helper) are inlined into `_extract.py` rather than imported, so the file is a single drop-on-disk artifact.

### `patterns.py` — CLI-side changes

- Keeps: `wildcard_pattern_to_regex`, `extract_literal_prefix`, `combine_rule_patterns`, `find_rule_patterns`, `RuleSpec`, `SnakefileNotFound`, `resolve_snakefile`.
- Loses (moves into `_extract.py`): `_strip_inline_constraints`, the body of `load_rule_specs`.
- Adds:
  - `_extract_script_path() -> Path` — resolves the absolute path of `_extract.py` next to `patterns.py` via `Path(__file__).parent`.
  - `run_extractor(pipeline_dir: Path, configfiles: Sequence[Path] = ()) -> list[RuleSpec]` — finds `python` via `shutil.which("python")`, runs the subprocess, parses JSON, returns `RuleSpec` objects. Translates known subprocess failures into the exit codes / messages defined below.
  - `load_rule_specs(pipeline_dir: Path, configfiles: Sequence[Path] = ()) -> list[RuleSpec]` — preserved as a thin wrapper around `run_extractor`, so existing third-party callers and the project's own test suite keep working without rewriting.

`find_rule_patterns` gains an optional `configfiles: Sequence[Path] = ()` parameter, passes it through to `load_rule_specs`.

### `cli.py` — new flag, threaded through

One new option on `scan`:

```
--configfile PATH    Snakemake configfile to pass to the extractor; repeatable.
```

Threaded into the call to `find_rule_patterns`. No other CLI surface changes.

### `pyproject.toml` — dependency move

`snakemake` moves from runtime dependencies to a dev/test extra (`[project.optional-dependencies] dev = ["snakemake>=9.0", ...]`). End users `pipx install snakeprune` and get a pure-Python tool; the test environment installs the `dev` extra so the existing tests can spin up the subprocess.

### `README.md` — doc updates in the same commit set

Bundled with the code so docs and behaviour land together:

- `Install`: lead with `pipx install snakeprune`, keep `pip install -e .` as the from-source path.
- `Runtime requirements`: rewrite — `snakemake` lives in the *workflow's* env, not snakeprune's. snakeprune calls into it via subprocess.
- `How it works`: rewrite steps 1–3 to describe the subprocess flow (find `python` on PATH; resolve `_extract.py`; subprocess emits JSON; CLI compiles regexes).
- `Limitations`: drop "Config-conditional rules" (now resolvable with `--configfile`).
- `Usage`: add a `--configfile` example.

## Data flow

1. User runs `snakeprune scan <pipeline> <results> [--configfile prod.yaml]` from inside their activated workflow env (so `python` on `PATH` is that env's Python).
2. CLI does the existing safety prechecks (Snakefile present, etc.).
3. CLI calls `find_rule_patterns(pipeline, configfiles=(...))`.
4. `find_rule_patterns` → `run_extractor` → `shutil.which("python")` resolves the interpreter, `_extract_script_path()` resolves the extractor file, and a single `subprocess.run([python, extract_path, str(pipeline), "--configfile", ...], capture_output=True, text=True, check=False)` runs the extractor.
5. On success: parse JSON → reconstruct `RuleSpec` objects → existing `wildcard_pattern_to_regex` builds anchored regexes → existing `combine_rule_patterns` collapses them.
6. The rest of the scan (walker, orphan classification, deletion flow) is unchanged.

## Error handling and exit codes

A new exit code, **4**, is introduced for "environment / subprocess problems" — distinct from `2` (pre-existing usage errors) and `3` (safety refusals added by the safety-hardening plan).

| Failure | Exit | Message (stderr) |
|---|---|---|
| `python` not on PATH | 4 | "Python interpreter `python` not found on PATH. Activate your workflow environment where you would normally run `snakemake`." |
| Subprocess fails and stderr contains `No module named 'snakemake'` | 4 | "The extractor failed: snakemake is not importable in `<python-path>`. Activate the env where you'd normally run `snakemake`." |
| Subprocess fails for any other reason (nonzero exit) | 4 | "Extractor failed (exit `<N>`):" followed by the subprocess's stderr verbatim. |
| Subprocess exits 0 but stdout isn't valid JSON | 4 | "Extractor produced unparseable output. This is a bug; please report. stderr was: `<subprocess stderr>`." |
| `--configfile` path doesn't exist | (passes through) | Snakemake itself reports the error inside the subprocess; surfaced via the "Extractor failed" path above. |
| Snakefile missing | 2 (unchanged) | Existing pre-check fires before any subprocess attempt; same message as today. |
| Empty rule list after extraction | 3 (unchanged) | The Task-3 refusal from the safety plan still fires; bypassable with `--allow-empty-rules`. |

## Testing

Existing tests largely keep working because `load_rule_specs` is preserved as a wrapper. New / changed tests:

- `tests/test_patterns.py`:
  - Existing `load_rule_specs_*` tests: unchanged. They now exercise the subprocess flow end-to-end because the test env has `snakemake` installed via the `dev` extra.
  - New test: `test_run_extractor_propagates_configfile` — Snakefile with a rule gated by `config["do_qc"]`; without `--configfile` the rule is absent, with it present.
  - New test: `test_run_extractor_when_python_missing_exits_with_helpful_error` — monkeypatch `shutil.which` to return `None`; assert exit 4 + message.
  - New test: `test_run_extractor_when_snakemake_not_importable_exits_with_helpful_error` — point the runner at a Python interpreter known not to have snakemake (e.g. spawn a venv in tmp_path with `venv.create(...)` and feed that python path through a private kwarg `_python_exe_for_testing`); assert exit 4 + message.
  - New test: `test_run_extractor_when_extractor_emits_bad_json_exits_with_helpful_error` — monkeypatch the script path to a tmp file that prints garbage; assert exit 4 + message.
- `tests/test_cli.py`:
  - New test: `test_cli_scan_forwards_configfile_to_extractor` — Snakefile with a config-gated rule; `--configfile` makes the gated rule visible (no orphans), absence of `--configfile` produces orphans.
- The subprocess overhead is a few hundred ms per invocation. Roughly 10 existing tests touch `load_rule_specs`, so expect +2–3s on the suite. Flag for monitoring; if it crosses ~5s sustained, consider a fixture-level extractor-result cache.

### Test seams

`run_extractor` accepts two private keyword-only arguments for tests:
- `_python_exe_for_testing: Path | None = None` — overrides the `shutil.which` lookup.
- `_script_path_for_testing: Path | None = None` — overrides the `_extract.py` path so a deliberately-broken script can be substituted.

These are private (leading underscore), undocumented in the public help, and exist solely to make the error paths testable without expensive shenanigans. They are not exposed via the CLI.

## Backwards compatibility

- **CLI:** `snakeprune scan` adds one new optional flag (`--configfile`); every existing invocation produces the same observable behaviour, modulo a small per-scan subprocess startup cost. No flag removed or renamed.
- **Library API:** `patterns.load_rule_specs(pipeline_dir)` keeps working with the same signature (the new `configfiles` param defaults to `()`). Third-party Python callers (if any) see the function still return `list[RuleSpec]`.
- **Distribution:** `pip install snakeprune` no longer pulls `snakemake` into the same env. Users who previously installed snakeprune *into* their workflow env still have snakemake there from their workflow's own deps; nothing changes for them functionally. Users who installed snakeprune in a dedicated env without snakemake will now succeed at install time and fail at first scan with a clear "snakemake not importable" message (instead of failing earlier with an `ImportError` at install / first-import time).

## Out of scope

- Publishing to PyPI / conda-forge (separate small project — the `pyproject.toml` change here makes it cleanly possible, but the actual `twine upload` workflow is not part of this spec).
- Caching extractor output between scans (subprocess cost is small enough not to be worth the cache-invalidation complexity).
- A `--python` override flag for selecting a non-PATH interpreter (rejected during brainstorming — `conda activate` is the assumed workflow).
- A `--config KEY=VALUE` inline-override flag (rejected during brainstorming — `--configfile` is sufficient).
- Forwarding other snakemake CLI flags (`--directory`, `--workdir`, etc.). Revisit if asked.
