# snakeprune Safety-Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the 2026-10-14 safety review so snakeprune cannot report a live Snakemake output as an orphan for brace-quantified constraints, `directory()` outputs, absolute paths, or cross-contaminated constraints, and so deletion failures are reported honestly.

**Architecture:** The extractor subprocess (`_extract.py`) stops parsing wildcard braces and emits raw annotated path strings plus a `directories` list; the single wildcard parser in `patterns.py` is rewritten to mirror Snakemake's `WILDCARD_REGEX` and to fail loudly on anything it cannot parse. The CLI gains `--permissive-wildcards`, a trash-inside-results refusal, an absolute-pattern warning, and exit code 5 for partial deletions. `walker.find_orphans` is removed.

**Tech Stack:** Python 3.12, typer, pytest, Snakemake 9.x (dev dependency; the extractor runs it in a subprocess). Run tests with `python -m pytest` from the repo root (`python` is the miniforge interpreter with snakemake installed).

**Spec:** `docs/superpowers/specs/2026-10-14-snakeprune-review-fixes-design.md`

## Global Constraints

- `src/snakeprune/_extract.py` must not import anything from `snakeprune` (it runs in the workflow's env, which may not have snakeprune installed). It may import from `snakemake.io`.
- Default wildcard regex stays `[^/]+`. `.+` is opt-in via `--permissive-wildcards`.
- Exit codes: 2 Snakefile not found; 3 refused before any destructive action; 4 extractor/pattern failure; 5 deletion started but did not complete.
- Every commit is followed by `git push` (user memory: auto-push after every commit).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Match existing test style: `make_pipeline` / `make_results` fixtures from `tests/conftest.py`, Snakefile text written as concatenated single-quoted lines, comments explaining the safety rationale.

## Review Focus

1. A rule whose constraint body contains a nested quantifier *and* a comma inside brackets, e.g. `{s,[a-z,]{1,3}}` — pinned in Task 1 (`test_wildcard_regex_constraint_with_comma_in_class`).
2. A `directory()` output with no wildcards at all (`directory("results/static")`) — `extract_literal_prefix` must still return `results/static` and files beneath must be live; pinned in Task 2.
3. `--permissive-wildcards` combined with a repeated wildcard (backreference) — equality semantics must survive; pinned in Task 3.
4. A workflow where *every* rule writes to an absolute path outside the workdir, so after dropping them the pattern list is empty — the existing empty-rule refusal must fire, not a crash; pinned in Task 4.
5. `--trash` equal to `results_dir` itself (not merely inside it) — must be refused; pinned in Task 6.

---

### Task 1: Single wildcard parser with brace-quantifier support and loud failure

**Files:**
- Modify: `src/snakeprune/_extract.py:27-40, 95-119`
- Modify: `src/snakeprune/patterns.py:11-40, 68-82, 173-200`
- Modify: `src/snakeprune/cli.py:12-18, 143-151`
- Test: `tests/test_patterns.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - Extractor JSON: `{"rules": [{"name": str, "outputs": list[str], "directories": list[str], "constraints": dict[str, str]}]}` where `outputs` are the **raw** strings from `str(o)` (inline `{name,regex}` annotations preserved) and `directories` is the subset of `outputs` flagged `directory()` (always emitted; populated in Task 2).
  - `RuleSpec(name: str, outputs: list[str], constraints: dict[str, str], directories: list[str] = [])`.
  - `wildcard_pattern_to_regex(pattern: str, constraints: dict[str, str], default: str = r"[^/]+", is_directory: bool = False) -> str`.
  - `class PatternError(ValueError)` in `patterns.py`, raised when a `{` or `}` survives in a literal segment.
  - `find_rule_patterns(pipeline_dir, configfiles=()) -> list[tuple[str, re.Pattern]]` (unchanged signature in this task; raises `PatternError`).

- [ ] **Step 1: Write the failing end-to-end test (global brace-quantifier constraint)**

Append to `tests/test_cli.py`:

```python
def test_cli_scan_brace_quantifier_constraint_is_live(make_pipeline, make_results):
    # Snakemake inlines global and rule-level wildcard_constraints into every
    # output string as `{name,regex}`. A regex with a brace quantifier such as
    # `[0-9]{1,2}` (chromosomes!) must be parsed, not escaped into a literal
    # that matches nothing — otherwise every file of the rule is an orphan and
    # --delete would remove live outputs.
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    chrom = '[0-9]{1,2}'\n"
        "\n"
        "rule a:\n"
        "    output: 'results/{chrom}.g.txt'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule b:\n"
        "    wildcard_constraints: sample = '[0-9]{2}'\n"
        "    output: 'results/{sample}.r.txt'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule c:\n"
        "    output: 'results/{sample,[a-z]{3}}.i.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["7.g.txt", "12.r.txt", "abc.i.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "orphan.csv" in result.stdout
    assert "7.g.txt" not in result.stdout
    assert "12.r.txt" not in result.stdout
    assert "abc.i.txt" not in result.stdout
```

- [ ] **Step 2: Run it to confirm it fails for the right reason**

Run: `python -m pytest tests/test_cli.py::test_cli_scan_brace_quantifier_constraint_is_live -v`
Expected: FAIL — `assert "7.g.txt" not in result.stdout` (the live files are listed as orphans).

- [ ] **Step 3: Write the unit tests for the new parser**

In `tests/test_patterns.py`, directly after `test_repeated_wildcard_with_constraint_uses_backreference`, add:

```python
def test_inline_constraint_is_parsed_from_pattern():
    # Snakemake-style `{name,regex}` annotations are parsed by patterns.py
    # itself; the extractor no longer strips them.
    regex = wildcard_pattern_to_regex("results/{x,[0-9]+}.txt", constraints={})
    assert regex == r"^results/(?P<x>[0-9]+)\.txt$"


def test_inline_constraint_with_brace_quantifier():
    # The constraint body may contain one level of `{n}` / `{n,m}` quantifier
    # braces, exactly as Snakemake's WILDCARD_REGEX allows.
    regex = wildcard_pattern_to_regex("results/{chrom,[0-9]{1,2}}.txt", constraints={})
    assert regex == r"^results/(?P<chrom>[0-9]{1,2})\.txt$"
    regex = wildcard_pattern_to_regex("results/{s,[a-z]{3}}.txt", constraints={})
    assert regex == r"^results/(?P<s>[a-z]{3})\.txt$"


def test_wildcard_regex_constraint_with_comma_in_class():
    # A comma inside a character class must not be mistaken for the
    # name/constraint separator; only the first comma after the name is.
    regex_str = wildcard_pattern_to_regex("{s,[a-z,]{1,3}}.txt", constraints={})
    pat = _re_module.compile(regex_str)
    assert pat.match("a,b.txt")
    assert not pat.match("abcd.txt")


def test_inline_constraint_beats_constraints_dict():
    # Snakemake precedence: inline annotation > rule-local > global. The
    # `constraints` dict (rule-local merged over global) is only a fallback for
    # wildcards that carry no inline annotation.
    regex = wildcard_pattern_to_regex(
        "{x,[0-9]+}/{y}.txt", constraints={"x": "[ab]+", "y": r"\d+"}
    )
    assert regex == r"^(?P<x>[0-9]+)/(?P<y>\d+)\.txt$"


def test_dict_constraint_with_brace_quantifier():
    regex = wildcard_pattern_to_regex("{n}.txt", constraints={"n": "[0-9]{2}"})
    assert regex == r"^(?P<n>[0-9]{2})\.txt$"


def test_default_wildcard_regex_is_overridable():
    regex = wildcard_pattern_to_regex("results/{n}.txt", constraints={}, default=".+")
    assert regex == r"^results/(?P<n>.+)\.txt$"


def test_unparseable_brace_raises_pattern_error():
    # Defence in depth: anything brace-shaped that the parser does not
    # recognise must raise, never be escaped into a literal that silently
    # matches nothing (which would turn every file of the rule into an orphan).
    from snakeprune.patterns import PatternError
    with pytest.raises(PatternError) as exc:
        wildcard_pattern_to_regex("results/{bad name}.txt", constraints={})
    assert "results/{bad name}.txt" in str(exc.value)
    with pytest.raises(PatternError):
        wildcard_pattern_to_regex("results/{x}}.txt", constraints={})
    with pytest.raises(PatternError):
        wildcard_pattern_to_regex("results/{x,[a-z]{3,}}}.txt", constraints={})
```

Note: `pytest` is imported further down in the file (line ~80, `import pytest`); move that `import pytest` line up to the top of the file so these tests can use it.

- [ ] **Step 4: Run the unit tests to confirm they fail**

Run: `python -m pytest tests/test_patterns.py -k "inline_constraint or brace_quantifier or comma_in_class or overridable or unparseable" -v`
Expected: FAIL (wrong regex strings; `PatternError` import error; `default` kwarg TypeError).

- [ ] **Step 5: Rewrite the parser in `patterns.py`**

Replace lines 11-40 of `src/snakeprune/patterns.py` (the `_WILDCARD_RE` definition and `wildcard_pattern_to_regex`) with:

```python
# Mirrors snakemake.io.WILDCARD_REGEX: a wildcard name, optionally followed by
# a comma and a constraint regex whose body may contain one level of nested
# `{n}` / `{n,m}` quantifier braces (e.g. `{chrom,[0-9]{1,2}}`).
_WILDCARD_RE = re.compile(
    r"\{\s*(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?:\s*,\s*(?P<constraint>(?:[^{}]+|\{\d+(?:,\d+)?\})*))?\s*\}"
)

DEFAULT_WILDCARD_REGEX = r"[^/]+"
SNAKEMAKE_WILDCARD_REGEX = r".+"


class PatternError(ValueError):
    """Raised when a rule path contains brace syntax the parser cannot read.

    Failing loudly here is a safety property: an unparsed `{...}` escaped into
    a literal would match nothing, silently turning every file of that rule
    into an orphan.
    """


def wildcard_pattern_to_regex(
    pattern: str,
    constraints: dict[str, str],
    default: str = DEFAULT_WILDCARD_REGEX,
    is_directory: bool = False,
) -> str:
    """Convert a Snakemake path pattern to an anchored regex string.

    Each `{wildcard}` or `{wildcard,regex}` placeholder on its first occurrence
    becomes a named capture group. The group body is, in order of precedence:
    the inline `regex` annotation, `constraints[wildcard]`, then `default`.
    This matches Snakemake (inline > rule-local > global). Subsequent
    occurrences of the same wildcard become backreferences `(?P=name)`,
    preserving Snakemake's implicit-equality semantics. All other characters
    are escaped for literal matching.

    `is_directory=True` appends `(?:/.*)?` so that files *inside* a
    `directory()` output are matched as live.

    Raises `PatternError` if any `{` or `}` survives in a literal segment.
    """
    parts: list[str] = []
    cursor = 0
    seen: set[str] = set()

    def _literal(text: str) -> str:
        if "{" in text or "}" in text:
            raise PatternError(
                f"Cannot parse wildcard syntax in rule path `{pattern}` "
                f"(unexpected brace in `{text}`). snakeprune refuses to "
                f"guess: an unparsed pattern would mark every file of this "
                f"rule as an orphan."
            )
        return re.escape(text)

    for match in _WILDCARD_RE.finditer(pattern):
        parts.append(_literal(pattern[cursor : match.start()]))
        name = match.group("name")
        if name in seen:
            parts.append(f"(?P={name})")
        else:
            inline = match.group("constraint")
            body = inline if inline else constraints.get(name, default)
            parts.append(f"(?P<{name}>{body})")
            seen.add(name)
        cursor = match.end()
    parts.append(_literal(pattern[cursor:]))
    suffix = r"(?:/.*)?" if is_directory else ""
    return "^" + "".join(parts) + suffix + "$"
```

- [ ] **Step 6: Run the unit tests to confirm they pass**

Run: `python -m pytest tests/test_patterns.py -k "wildcard or inline_constraint or brace_quantifier or comma_in_class or overridable or unparseable" -v`
Expected: PASS.

- [ ] **Step 7: Stop parsing braces in the extractor**

In `src/snakeprune/_extract.py`:

Delete `_INLINE_CONSTRAINT_RE` and `_strip_inline_constraints` (lines 27-40). Update the module docstring lines 6-11 to:

```
It loads the Snakemake workflow at <pipeline_dir>, iterates over each path a
rule produces (output, log, benchmark), and emits the result as a JSON document
on stdout:

    {"rules": [{"name": str,
                "outputs": list[str],       # raw path strings, inline {name,regex} kept
                "directories": list[str],   # subset of outputs flagged directory()
                "constraints": dict[str, str]}, ...]}

Paths are emitted exactly as Snakemake stores them: Snakemake has already
inlined rule-level and global wildcard_constraints into each string as
`{name,regex}`. `constraints` (rule-local merged over global) is kept only as
a fallback for wildcards that carry no inline annotation. All brace parsing
happens in snakeprune's patterns.py — deliberately in one place.
```

Replace the per-rule loop body (lines 95-119) with:

```python
            produced = list(rule.output) + list(rule.log)
            if rule.benchmark:
                produced.append(rule.benchmark)
            raw_outputs = list(dict.fromkeys(str(o) for o in produced))
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "directories": [],
                    "constraints": {**global_constraints, **rule_constraints},
                }
            )
```

(`directories` is populated in Task 2; `dict.fromkeys` is an order-preserving dedup so a `log:` path equal to an output is emitted once.)

- [ ] **Step 8: Thread the new fields through `patterns.py`**

In `src/snakeprune/patterns.py`:

Change `RuleSpec` to:

```python
@dataclass(frozen=True)
class RuleSpec:
    """Raw rule path information extracted from a loaded workflow.

    Fields:
        name: the rule name as declared in the Snakefile.
        outputs: raw path strings for everything the rule produces (``output``,
            ``log`` and ``benchmark``), exactly as Snakemake stores them —
            inline ``{name,regex}`` constraint annotations are preserved.
        constraints: fallback wildcard constraints (rule-local merged over
            workflow-global) for wildcards with no inline annotation.
        directories: the subset of ``outputs`` declared with ``directory()``.
    """
    name: str
    outputs: list[str]
    constraints: dict[str, str]
    directories: list[str] = field(default_factory=list)
```

and change `from dataclasses import dataclass` to `from dataclasses import dataclass, field`.

In `run_extractor`, build specs with:

```python
    return [
        RuleSpec(
            name=r["name"],
            outputs=list(r["outputs"]),
            constraints=dict(r["constraints"]),
            directories=list(r.get("directories", [])),
        )
        for r in payload["rules"]
    ]
```

In `find_rule_patterns`, change the inner loop to:

```python
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(
                output_str,
                spec.constraints,
                is_directory=output_str in spec.directories,
            )
            out.append((spec.name, re.compile(regex_str)))
    return out
```

and update its docstring first line to `"""Top-level: return one (rule_name, compiled_regex) per produced path."""`.

- [ ] **Step 9: Map `PatternError` to exit 4 in the CLI**

In `src/snakeprune/cli.py`, add `PatternError` to the `from snakeprune.patterns import (...)` list, and change the `except ExtractorError as exc:` clause (line 149) to `except (ExtractorError, PatternError) as exc:`.

- [ ] **Step 10: Update the extractor tests that asserted stripping**

In `tests/test_patterns.py`:

Replace `test_load_rule_specs_inline_constraint_captured` with:

```python
def test_load_rule_specs_inline_constraint_preserved_raw(make_pipeline):
    # The extractor emits the path exactly as Snakemake stores it; parsing the
    # `{name,regex}` annotation is patterns.py's job (single parser).
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    assert specs[0].outputs == ["results/{x,[0-9]+}.txt"]
```

Replace `test_load_rule_specs_inline_constraint_overridden_by_rule_local` with:

```python
def test_find_rule_patterns_inline_constraint_wins_over_rule_local(make_pipeline):
    # Snakemake precedence: an inline `{x,regex}` annotation beats the rule's
    # wildcard_constraints block. snakeprune must agree, or it would accept a
    # different set of files than Snakemake would produce.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.txt'\n"
        "    wildcard_constraints:\n"
        "        x = r'[ab]+'\n"
        "    shell: 'touch {output}'\n"
    )
    (_, regex), = find_rule_patterns(pipeline)
    assert regex.match("results/123.txt")
    assert not regex.match("results/ab.txt")
```

(`find_rule_patterns` is imported later in the file at line ~231; move that import up next to the `load_rule_specs` import.)

Replace `test_extract_script_strips_inline_constraints` with:

```python
def test_extract_script_emits_raw_annotated_paths_and_fallback_constraints(make_pipeline):
    # Snakemake inlines global constraints into the path string; the extractor
    # must pass that through untouched and also emit the fallback dict.
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    chrom = '[0-9]{1,2}'\n"
        "\n"
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.{chrom}.txt'\n"
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
    assert rule["outputs"] == ["results/{x,[0-9]+}.{chrom,[0-9]{1,2}}.txt"]
    assert rule["constraints"] == {"chrom": "[0-9]{1,2}"}
    assert rule["directories"] == []
```

Extend `test_load_rule_specs_module_with_prefix_applied` so the imported rule carries a global constraint, proving module/prefix rules get constraints inlined by the same path:

```python
def test_load_rule_specs_module_with_prefix_applied(make_pipeline):
    """`module foo: prefix: 'subdir/'` prepends a path to the imported rule's
    inputs and outputs. snakeprune must see the *prefixed* pattern, otherwise
    every file under `subdir/results/` would look like an orphan. The inlined
    constraint must survive the prefixing too."""
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    n = '[0-9]{2}'\n"
        "\n"
        "module foo:\n"
        "    snakefile: 'foo.smk'\n"
        "    prefix: 'subdir/'\n"
        "\n"
        "use rule * from foo\n",
        smk_files={
            "foo.smk":
                "rule a:\n"
                "    output: 'results/{n}.txt'\n"
                "    shell: 'touch {output}'\n"
        },
    )
    specs = load_rule_specs(pipeline)
    a = next(s for s in specs if s.name == "a")
    assert a.outputs == ["subdir/results/{n,[0-9]{2}}.txt"], a.outputs
```

If that last assertion fails because Snakemake does not inline global constraints into module-imported rules, change the assertion to `assert a.outputs == ["subdir/results/{n}.txt"]` and `assert a.constraints.get("n") == "[0-9]{2}"` — either way the fallback dict makes the regex correct, and the test must document which it is.

Add a CLI test for the loud-failure path in `tests/test_cli.py`:

```python
def test_cli_scan_unparseable_pattern_exits_4(make_pipeline, make_results, monkeypatch):
    # If the extractor ever hands back a brace sequence the parser does not
    # understand, the scan must stop with exit 4 — never escape it into a
    # literal that flags every file of the rule as an orphan.
    from snakeprune.patterns import RuleSpec
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    monkeypatch.setattr(
        "snakeprune.patterns.load_rule_specs",
        lambda *a, **k: [RuleSpec(name="a", outputs=["results/{bad name}.txt"], constraints={})],
    )
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 4
    assert "Cannot parse wildcard syntax" in result.output
    assert "1.txt" not in result.stdout
```

- [ ] **Step 11: Run the whole suite**

Run: `python -m pytest -q`
Expected: all PASS, including `test_cli_scan_brace_quantifier_constraint_is_live`.

- [ ] **Step 12: Commit and push**

```bash
git add src/snakeprune/_extract.py src/snakeprune/patterns.py src/snakeprune/cli.py tests/test_patterns.py tests/test_cli.py docs/superpowers/specs/2026-10-14-snakeprune-review-fixes-design.md docs/superpowers/plans/2026-10-14-snakeprune-review-fixes.md
git commit -m "fix(patterns): parse brace-quantified constraints in one place, fail loudly on unknown braces

Snakemake inlines rule-level and global wildcard_constraints into every
produced path as {name,regex}. The extractor's stripping regex could not
read a body containing {n} or {n,m}, so any quantified constraint (e.g.
chrom='[0-9]{1,2}') was escaped into a literal that matched nothing and
every file of the rule became an orphan. The extractor now emits raw
strings; patterns.py owns the single parser (mirroring Snakemake's
WILDCARD_REGEX), applies inline > rule-local > global precedence per
path, and raises PatternError (exit 4) on anything it cannot parse.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 2: Files inside `directory()` outputs are live

**Files:**
- Modify: `src/snakeprune/_extract.py` (per-rule loop from Task 1)
- Test: `tests/test_patterns.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `RuleSpec.directories`, `wildcard_pattern_to_regex(..., is_directory=True)` from Task 1.
- Produces: extractor populates `"directories"` with the `str(o)` of every `rule.output` item for which `snakemake.io.is_flagged(o, "directory")` is true.

- [ ] **Step 1: Write the failing CLI test**

Append to `tests/test_cli.py`:

```python
def test_cli_scan_files_inside_directory_output_are_live(make_pipeline, make_results):
    # A directory() output is produced as a whole; every file inside it —
    # including Snakemake's own .snakemake_timestamp marker — is live. Deleting
    # them would corrupt an output Snakemake still believes is complete.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: directory('results/{sample}/outdir')\n"
        "    shell: 'mkdir -p {output}'\n"
        "\n"
        "rule b:\n"
        "    output: directory('results/static')\n"
        "    shell: 'mkdir -p {output}'\n"
    )
    results = make_results(
        [
            "A/outdir/x.txt",
            "A/outdir/deep/y.txt",
            "A/outdir/.snakemake_timestamp",
            "static/z.txt",
            "A/other.txt",
        ]
    )
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "other.txt" in result.stdout
    assert "x.txt" not in result.stdout
    assert "y.txt" not in result.stdout
    assert ".snakemake_timestamp" not in result.stdout
    assert "z.txt" not in result.stdout
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_cli.py::test_cli_scan_files_inside_directory_output_are_live -v`
Expected: FAIL — `x.txt` etc. appear in stdout as orphans.

- [ ] **Step 3: Write the unit tests**

In `tests/test_patterns.py`, after `test_default_wildcard_regex_is_overridable`, add:

```python
def test_directory_pattern_matches_contents():
    regex_str = wildcard_pattern_to_regex(
        "results/{s}/outdir", constraints={}, is_directory=True
    )
    assert regex_str == r"^results/(?P<s>[^/]+)/outdir(?:/.*)?$"
    pat = _re_module.compile(regex_str)
    assert pat.match("results/A/outdir")
    assert pat.match("results/A/outdir/x.txt")
    assert pat.match("results/A/outdir/deep/.snakemake_timestamp")
    assert not pat.match("results/A/outdir2/x.txt")


def test_directory_pattern_without_wildcards_keeps_literal_prefix():
    from snakeprune.patterns import extract_literal_prefix
    regex_str = wildcard_pattern_to_regex("results/static", constraints={}, is_directory=True)
    compiled = _re_module.compile(regex_str)
    assert compiled.match("results/static/z.txt")
    assert extract_literal_prefix(compiled) == "results/static"


def test_load_rule_specs_reports_directory_outputs(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output:\n"
        "        d = directory('results/{s}/outdir'),\n"
        "        f = 'results/{s}/flat.txt'\n"
        "    shell: 'mkdir -p {output.d}; touch {output.f}'\n"
    )
    specs = load_rule_specs(pipeline)
    assert specs[0].directories == ["results/{s}/outdir"]
    assert set(specs[0].outputs) == {"results/{s}/outdir", "results/{s}/flat.txt"}
```

- [ ] **Step 4: Run them to confirm the extractor test fails**

Run: `python -m pytest tests/test_patterns.py -k "directory" -v`
Expected: the two pure-regex tests PASS (Task 1 already implemented the suffix); `test_load_rule_specs_reports_directory_outputs` FAILS (`directories == []`).

- [ ] **Step 5: Populate `directories` in the extractor**

In `src/snakeprune/_extract.py`, inside `_load_workflow`, add `from snakemake.io import is_flagged` next to the other snakemake imports, and change the per-rule block to:

```python
            produced = list(rule.output) + list(rule.log)
            if rule.benchmark:
                produced.append(rule.benchmark)
            raw_outputs = list(dict.fromkeys(str(o) for o in produced))
            directories = [str(o) for o in rule.output if is_flagged(o, "directory")]
            rule_constraints = dict(
                getattr(rule, "wildcard_constraints", {}) or {}
            )
            rules_out.append(
                {
                    "name": rule.name,
                    "outputs": raw_outputs,
                    "directories": directories,
                    "constraints": {**global_constraints, **rule_constraints},
                }
            )
```

- [ ] **Step 6: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/snakeprune/_extract.py tests/test_patterns.py tests/test_cli.py
git commit -m "fix(patterns): treat files inside directory() outputs as live

A directory() output's pattern only matched the directory path itself, so
every file inside — including Snakemake's .snakemake_timestamp — was
reported as an orphan. The extractor now flags directory outputs and the
pattern gets an optional (?:/.*)? suffix.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 3: `--permissive-wildcards` and an honest README claim

**Files:**
- Modify: `src/snakeprune/patterns.py` (`find_rule_patterns`)
- Modify: `src/snakeprune/cli.py` (new option; pass-through)
- Modify: `README.md` (Usage, "How it works" step 3, Limitations)
- Test: `tests/test_patterns.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `find_rule_patterns(pipeline_dir, configfiles=(), default_wildcard_regex=DEFAULT_WILDCARD_REGEX)`; CLI flag `--permissive-wildcards` sets it to `SNAKEMAKE_WILDCARD_REGEX`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_cli_scan_permissive_wildcards_matches_slash_in_wildcard(make_pipeline, make_results):
    # Snakemake's own default wildcard regex is `.+`, which spans `/`. A file
    # Snakemake produced with sample='x/y' is live under --permissive-wildcards
    # (and, deliberately, an orphan under the stricter default — see README).
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}/flat.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["x/y/flat.txt", "x/flat.txt"])
    strict = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert strict.exit_code == 0, strict.output
    assert "x/y/flat.txt" in strict.stdout
    permissive = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--permissive-wildcards"]
    )
    assert permissive.exit_code == 0, permissive.output
    assert "flat.txt" not in permissive.stdout
```

Append to `tests/test_patterns.py` after `test_find_rule_patterns_compiles_with_constraints`:

```python
def test_find_rule_patterns_permissive_default_keeps_backreference_equality(make_pipeline):
    # `.+` as the default must not break implicit equality of repeated
    # wildcards: {x}/{x} still requires both occurrences to be identical.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x}/{x}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    (_, regex), = find_rule_patterns(pipeline, default_wildcard_regex=".+")
    assert regex.match("results/a/b/a/b.txt")
    assert not regex.match("results/a/b/c.txt")
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_cli.py::test_cli_scan_permissive_wildcards_matches_slash_in_wildcard tests/test_patterns.py::test_find_rule_patterns_permissive_default_keeps_backreference_equality -v`
Expected: FAIL (unknown option / unexpected kwarg).

- [ ] **Step 3: Implement**

`src/snakeprune/patterns.py`, `find_rule_patterns`:

```python
def find_rule_patterns(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    default_wildcard_regex: str = DEFAULT_WILDCARD_REGEX,
) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per produced path.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern. `default_wildcard_regex` is the body used for
    wildcards with no constraint at all (default `[^/]+`; pass
    `SNAKEMAKE_WILDCARD_REGEX` for Snakemake's own `.+`).
    """
    resolve_snakefile(pipeline_dir)
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            regex_str = wildcard_pattern_to_regex(
                output_str,
                spec.constraints,
                default=default_wildcard_regex,
                is_directory=output_str in spec.directories,
            )
            out.append((spec.name, re.compile(regex_str)))
    return out
```

`src/snakeprune/cli.py`: import `DEFAULT_WILDCARD_REGEX, SNAKEMAKE_WILDCARD_REGEX` from `snakeprune.patterns`; add after the `exclude_dir` option:

```python
    permissive_wildcards: bool = typer.Option(
        False,
        "--permissive-wildcards",
        help="Use Snakemake's own `.+` for unconstrained wildcards (spans `/`). "
             "Default is the stricter `[^/]+`, which is what lets snakeprune "
             "detect files from older, differently-nested output layouts.",
    ),
```

and call:

```python
        patterns = find_rule_patterns(
            pipeline_dir,
            configfiles=tuple(configfile or ()),
            default_wildcard_regex=(
                SNAKEMAKE_WILDCARD_REGEX if permissive_wildcards else DEFAULT_WILDCARD_REGEX
            ),
        )
```

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 5: Fix the README**

In `README.md`:

In "How it works" step 3, replace `missing constraints default to `[^/]+`)` with `missing constraints default to `[^/]+`, or Snakemake's own `.+` under `--permissive-wildcards`)`.

In the Usage block, add after the `--exclude-dir` example:

```bash
# Let unconstrained wildcards span '/' (Snakemake's own default); fewer orphans
snakeprune scan path/to/pipeline path/to/results --permissive-wildcards
```

In "Limitations", add a bullet before "Config-conditional rules":

```markdown
- **Unconstrained wildcards are stricter than Snakemake's.** Snakemake lets an unconstrained `{wildcard}` match `.+`, which spans `/`. `snakeprune` deliberately uses `[^/]+` instead: with `.+`, a rule writing `results/{sample}/{replicate}/out.txt` would also claim `results/A/B/norm/out.txt`, hiding exactly the stale deeper layouts this tool exists to find. The cost is that a file Snakemake produced with a `/` inside a wildcard value (e.g. `sample=batch1/s1`) is reported as an orphan. If your workflow does that, pass `--permissive-wildcards`, or add a `wildcard_constraints` block so the two tools agree.
```

- [ ] **Step 6: Commit and push**

```bash
git add src/snakeprune/patterns.py src/snakeprune/cli.py README.md tests/test_patterns.py tests/test_cli.py
git commit -m "feat(cli): add --permissive-wildcards; document stricter default honestly

The README claimed [^/]+ matched Snakemake's default; Snakemake uses .+.
Keep [^/]+ (it is what finds stale nested layouts) but say so, and let
users opt into Snakemake's semantics.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 4: Absolute output paths — relativise under workdir, warn and drop otherwise

**Files:**
- Modify: `src/snakeprune/_extract.py` (`_load_workflow`)
- Modify: `src/snakeprune/patterns.py` (`find_rule_patterns`)
- Modify: `src/snakeprune/cli.py` (warning callback)
- Test: `tests/test_patterns.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `find_rule_patterns` signature.
- Produces: `find_rule_patterns(..., on_warning: Callable[[str], None] | None = None)`; extractor emits workdir-relative strings for absolute paths under the workdir.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_patterns.py`:

```python
def test_run_extractor_relativises_absolute_paths_under_workdir(make_pipeline):
    # A rule that builds its output from an absolute path (typical with
    # config['outdir'] = os.path.abspath(...)) still writes under the project.
    # The extractor must emit it workdir-relative so it can match
    # `results/<rel>`; otherwise every file of that rule is an orphan.
    pipeline = make_pipeline(
        "import os\n"
        "OUT = os.path.abspath('results')\n"
        "rule a:\n"
        "    output: OUT + '/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert specs[0].outputs == ["results/{n}.txt"]


def test_run_extractor_leaves_foreign_absolute_paths_absolute(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert specs[0].outputs == ["/nowhere/elsewhere/{n}.txt"]


def test_find_rule_patterns_drops_absolute_patterns_with_warning(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
        "rule b:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    warnings: list[str] = []
    patterns = find_rule_patterns(pipeline, on_warning=warnings.append)
    assert [name for name, _ in patterns] == ["b"]
    assert len(warnings) == 1
    assert "/nowhere/elsewhere/{n}.txt" in warnings[0]
    assert "rule `a`" in warnings[0]
```

Append to `tests/test_cli.py`:

```python
def test_cli_scan_absolute_output_under_project_is_live(make_pipeline, make_results):
    pipeline = make_pipeline(
        "import os\n"
        "OUT = os.path.abspath('results')\n"
        "rule a:\n"
        "    output: OUT + '/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "orphan.csv" in result.stdout
    assert "1.txt" not in result.stdout


def test_cli_scan_foreign_absolute_output_warns_even_when_quiet(make_pipeline, make_results):
    # Dropping a pattern narrows the live set, which is safety-relevant, so the
    # warning is not subject to --quiet.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
        "rule b:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--quiet"])
    assert result.exit_code == 0, result.output
    assert "/nowhere/elsewhere/{n}.txt" in result.output
    assert "WARNING" in result.output


def test_cli_scan_all_rules_foreign_absolute_hits_empty_rule_refusal(make_pipeline, make_results):
    # After dropping every absolute pattern nothing is left; the existing
    # empty-rule refusal (exit 3) must fire rather than scanning with no
    # patterns and reporting every file as an orphan.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 3
    assert "Refusing to scan" in result.output
```

- [ ] **Step 2: Run them to confirm failure**

Run: `python -m pytest tests/test_patterns.py tests/test_cli.py -k "absolute" -v`
Expected: FAIL (absolute string emitted raw; unknown kwarg `on_warning`; CLI mismatch refusal fires instead of the empty refusal).

- [ ] **Step 3: Relativise in the extractor**

In `src/snakeprune/_extract.py`, add a helper above `_load_workflow`:

```python
def _relativise(path: str, workdir: Path) -> str:
    """Strip the workdir prefix from an absolute path that lies under it.

    Both the plain and the symlink-resolved form of the workdir are tried,
    because a Snakefile that calls `os.path.abspath` yields whichever form the
    subprocess's cwd happens to have. Absolute paths elsewhere are returned
    unchanged; `patterns.py` drops them with a warning.
    """
    import os

    if not os.path.isabs(path):
        return path
    for base in {os.path.abspath(workdir), str(Path(workdir).resolve())}:
        prefix = base.rstrip(os.sep) + os.sep
        if path.startswith(prefix):
            return path[len(prefix):]
    return path
```

and in the per-rule block change `raw_outputs = list(dict.fromkeys(str(o) for o in produced))` to `raw_outputs = list(dict.fromkeys(_relativise(str(o), workdir) for o in produced))` and `directories = [str(o) for o in ...]` to `directories = [_relativise(str(o), workdir) for o in rule.output if is_flagged(o, "directory")]`.

- [ ] **Step 4: Drop remaining absolutes in `find_rule_patterns`**

```python
from typing import Callable, Sequence

def find_rule_patterns(
    pipeline_dir: Path,
    configfiles: Sequence[Path] = (),
    default_wildcard_regex: str = DEFAULT_WILDCARD_REGEX,
    on_warning: Callable[[str], None] | None = None,
) -> list[tuple[str, re.Pattern]]:
    """Top-level: return one (rule_name, compiled_regex) per produced path.

    Rules with multiple outputs (e.g., multiext) contribute multiple entries, one
    per output file pattern. `default_wildcard_regex` is the body used for
    wildcards with no constraint at all (default `[^/]+`; pass
    `SNAKEMAKE_WILDCARD_REGEX` for Snakemake's own `.+`).

    Absolute paths that the extractor could not relativise against the workdir
    can never match `<results-dir-name>/<rel>`; they are skipped and reported
    through `on_warning` (if given) so the user knows the live set is narrower
    than the workflow.
    """
    resolve_snakefile(pipeline_dir)
    out: list[tuple[str, re.Pattern]] = []
    for spec in load_rule_specs(pipeline_dir, configfiles=configfiles):
        for output_str in spec.outputs:
            if output_str.startswith("/"):
                if on_warning is not None:
                    on_warning(
                        f"WARNING: dropping absolute path `{output_str}` from "
                        f"rule `{spec.name}`: it lies outside the workflow "
                        f"directory and can never match a scanned file. Files "
                        f"it produces under the results dir would be reported "
                        f"as orphans."
                    )
                continue
            regex_str = wildcard_pattern_to_regex(
                output_str,
                spec.constraints,
                default=default_wildcard_regex,
                is_directory=output_str in spec.directories,
            )
            out.append((spec.name, re.compile(regex_str)))
    return out
```

- [ ] **Step 5: Wire the CLI**

In `cli.py`, pass `on_warning=lambda msg: typer.echo(msg, err=True)` to `find_rule_patterns` (not the `log` helper — it must bypass `--quiet`).

- [ ] **Step 6: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/snakeprune/_extract.py src/snakeprune/patterns.py src/snakeprune/cli.py tests/test_patterns.py tests/test_cli.py
git commit -m "fix(extract): relativise absolute outputs under workdir, warn on the rest

An absolute output path never matched the <results-name>/<rel> target,
so every file of such a rule was an orphan. Paths under the workdir are
now emitted relative; others are dropped with a non-quietable warning.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 5: Honest reporting of partial deletion (exit 5)

**Files:**
- Modify: `src/snakeprune/delete.py`
- Modify: `src/snakeprune/cli.py:275-284`
- Modify: `README.md` (Deletion flow)
- Test: `tests/test_delete.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `class DeletionFailed(RuntimeError)` with attributes `completed: int`, `total: int`, `path: Path`, raised by `delete_orphans` when an `OSError` occurs during the execution loop.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_delete.py`:

```python
from snakeprune.delete import DeletionFailed


def test_delete_orphans_mid_batch_failure_reports_progress(tmp_path, monkeypatch):
    # Validation is all-or-nothing, but once unlinking starts a failure can
    # strike half-way. The caller must learn exactly how many files are already
    # gone — "Refusing to delete" after the fact would be a lie.
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    for f in (first, second, third):
        f.write_text("data")
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == second:
            raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(
            [
                OrphanFile(path=first, rel="first.txt"),
                OrphanFile(path=second, rel="second.txt"),
                OrphanFile(path=third, rel="third.txt"),
            ]
        )
    assert exc.value.completed == 1
    assert exc.value.total == 3
    assert exc.value.path == second
    assert "1 of 3" in str(exc.value)
    assert not first.exists()
    assert second.exists()
    assert third.exists()


def test_delete_orphans_trash_mid_batch_failure_reports_progress(tmp_path, monkeypatch):
    import shutil as _shutil
    src = tmp_path / "src"
    src.mkdir()
    a = src / "a.txt"
    b = src / "b.txt"
    a.write_text("a")
    b.write_text("b")
    trash = tmp_path / "trash"
    real_move = _shutil.move

    def flaky_move(s, d, *args, **kwargs):
        if str(s) == str(b):
            raise OSError("disk full")
        return real_move(s, d, *args, **kwargs)

    monkeypatch.setattr("snakeprune.delete.shutil.move", flaky_move)
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(
            [OrphanFile(path=a, rel="a.txt"), OrphanFile(path=b, rel="b.txt")],
            trash_dir=trash,
            results_dir_name="src",
        )
    assert exc.value.completed == 1
    assert (trash / "src" / "a.txt").exists()
    assert b.exists()
```

Append to `tests/test_cli.py`:

```python
def test_cli_scan_delete_partial_failure_exits_5_with_count(
    make_pipeline, make_results, monkeypatch
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "o1.csv", "o2.csv", "o3.csv"])
    from pathlib import Path as _P
    real_unlink = _P.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(_P, "unlink", flaky_unlink)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 5
    assert "Deleted 1 of 3" in result.output
    assert "Refusing" not in result.output
    assert (results / "1.txt").exists()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_delete.py tests/test_cli.py -k "partial or mid_batch" -v`
Expected: FAIL (`ImportError: DeletionFailed`; CLI exits 3 with "Refusing").

- [ ] **Step 3: Implement**

`src/snakeprune/delete.py`, add after the imports:

```python
class DeletionFailed(RuntimeError):
    """Raised when a delete/trash batch fails part-way through execution.

    Validation is all-or-nothing, but the execution loop can still hit an
    ``OSError`` (permissions changed, disk full, file vanished). The exception
    records how far the batch got so the CLI can tell the user exactly what
    is already gone.
    """

    def __init__(self, completed: int, total: int, path: Path, cause: OSError):
        self.completed = completed
        self.total = total
        self.path = path
        super().__init__(
            f"Deleted {completed} of {total} file(s) before failing at "
            f"{path}: {cause}. The files listed before it are already gone; "
            f"the rest were not touched."
        )
```

Replace the execution loop (lines 53-63) with:

```python
    total = len(orphans)
    for completed, orphan in enumerate(orphans):
        path = orphan.path
        try:
            if trash_dir is not None:
                assert results_dir_name is not None  # for type-checkers
                target = trash_dir / results_dir_name / orphan.rel
                target.parent.mkdir(parents=True, exist_ok=True)
                print(f"moving to trash: {path} -> {target}", file=sys.stderr)
                shutil.move(str(path), str(target))
            else:
                print(f"deleting: {path}", file=sys.stderr)
                path.unlink()
        except OSError as exc:
            raise DeletionFailed(completed, total, path, exc) from exc
```

Update the docstring's last paragraph to add: `If an ``OSError`` occurs during execution, ``DeletionFailed`` is raised with the count of entries already removed.`

`src/snakeprune/cli.py`: import `DeletionFailed` from `snakeprune.delete`, and replace lines 275-284 with:

```python
        try:
            delete_orphans(
                orphans,
                allow_symlinks=allow_symlinks,
                trash_dir=trash,
                results_dir_name=results_dir.name if trash is not None else None,
            )
        except DeletionFailed as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=5)
        except (PermissionError, IsADirectoryError, FileExistsError) as exc:
            typer.echo(f"Refusing to delete: {exc}", err=True)
            raise typer.Exit(code=3)
```

(`DeletionFailed` must be caught first; it is not a subclass of the refusal tuple, but ordering makes intent explicit.)

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 5: Document exit codes in README**

In `README.md` under "### Deletion flow", append:

```markdown
- If a deletion batch fails part-way (permissions, disk full, file vanished), `snakeprune` stops, prints `Deleted N of M file(s) before failing at <path>`, and exits 5. Files before `<path>` are gone; files after it were not touched.

### Exit codes

| code | meaning |
|---|---|
| 0 | success, or you answered `n` at the prompt |
| 2 | no Snakefile found |
| 3 | refused by a safety guard before anything was deleted |
| 4 | the workflow could not be loaded, or a rule path could not be parsed |
| 5 | deletion started but did not complete (message says how many were removed) |
```

- [ ] **Step 6: Commit and push**

```bash
git add src/snakeprune/delete.py src/snakeprune/cli.py README.md tests/test_delete.py tests/test_cli.py
git commit -m "fix(delete): report partial deletion honestly with exit 5

A mid-batch OSError either printed 'Refusing to delete' after files were
already gone or escaped as a traceback. The loop now raises
DeletionFailed with the completed count; the CLI prints it and exits 5.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 6: Refuse `--trash` inside (or equal to) the results directory

**Files:**
- Modify: `src/snakeprune/cli.py` (after the `--limit` refusal, before the naughty-dir check)
- Modify: `README.md` (Built-in refusals)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_cli_scan_trash_inside_results_dir_refuses(make_pipeline, make_results):
    # Trashing into the scanned tree means the next scan reports the trash as
    # orphans and --delete would then unlink the only copy.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(results / ".trash"), "--yes"],
    )
    assert result.exit_code == 3
    assert "Refusing" in result.output
    assert "--trash" in result.output
    assert (results / "orphan.csv").exists()


def test_cli_scan_trash_equal_to_results_dir_refuses(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--trash", str(results), "--yes"]
    )
    assert result.exit_code == 3
    assert (results / "orphan.csv").exists()


def test_cli_scan_trash_as_ancestor_of_results_dir_collides_and_refuses(
    make_pipeline, make_results, tmp_path
):
    # --trash pointing at the *parent* of results_dir makes the trash target
    # `<parent>/results/<rel>` == the source file. The existing collision guard
    # refuses before anything moves. Pinned so a future refactor of the guard
    # cannot silently allow a self-move.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(tmp_path), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3
    assert "already exists" in result.output
    assert (results / "orphan.csv").exists()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_cli.py -k "trash_inside or trash_equal or trash_as_ancestor" -v`
Expected: the first two FAIL (orphan rate refusal or move succeeds); the third may already PASS.

- [ ] **Step 3: Implement**

In `cli.py`, after the `--limit` refusal block, add:

```python
    if trash is not None:
        results_abs = os.path.abspath(results_dir)
        trash_abs = os.path.abspath(trash)
        if trash_abs == results_abs or trash_abs.startswith(results_abs + os.sep):
            typer.echo(
                "Refusing to delete: --trash must not be inside the results "
                "directory being scanned. The next scan would report the "
                "trashed files as orphans and --delete would unlink the only "
                "copy. Choose a trash directory outside the results tree.",
                err=True,
            )
            raise typer.Exit(code=3)
```

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 5: README**

Under "### Built-in refusals", add:

```markdown
- **Trash inside the results tree.** `--trash DIR` is refused if `DIR` is the results directory or anywhere beneath it, because the next scan would report the trashed files as orphans.
```

- [ ] **Step 6: Commit and push**

```bash
git add src/snakeprune/cli.py README.md tests/test_cli.py
git commit -m "feat(cli): refuse --trash inside the scanned results directory

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 7: Remove the unguarded `walker.find_orphans` entry point

**Files:**
- Modify: `src/snakeprune/walker.py:109-160`
- Modify: `tests/test_walker.py` (remove the five `find_orphans` tests and its import)

- [ ] **Step 1: Confirm nothing else uses it**

Run: `grep -rn "find_orphans" src tests README.md docs`
Expected: hits only in `walker.py` and `tests/test_walker.py` (and possibly historical spec/plan docs, which are left alone).

- [ ] **Step 2: Delete the function and dead imports**

In `src/snakeprune/walker.py`, delete `find_orphans` (lines 140-160) and the now-unused imports `combine_rule_patterns, find_rule_patterns` from the `from snakeprune.patterns import (...)` block (keep `extract_literal_prefix`). Also delete the `Iterable` import only if no longer used (it is still used by `iter_results_files` — keep it).

- [ ] **Step 3: Delete its tests**

In `tests/test_walker.py` remove `test_find_orphans_distinguishes_live_and_orphan`, `test_find_orphans_empty_results`, `test_find_orphans_ignore_globs_excluded_from_orphan_set`, `test_find_orphans_with_attribution_guesses_closest_rule`, `test_find_orphans_sets_rel_on_orphan_file`, and change `from snakeprune.walker import find_orphans, OrphanFile` to `from snakeprune.walker import OrphanFile`. If `make_pipeline` is now unused in that file, leave the fixture import alone (fixtures are injected by name; nothing to remove).

- [ ] **Step 4: Run the suite and a lint**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: all PASS; ruff reports no unused imports in `walker.py`/`test_walker.py` (fix any it does report in those two files only).

- [ ] **Step 5: Commit and push**

```bash
git add src/snakeprune/walker.py tests/test_walker.py
git commit -m "refactor(walker): remove unused find_orphans entry point

It bypassed every CLI safety refusal and had no callers.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 8: Extractor hygiene — callable guard, wording, named-log and `repeat()` tests

**Files:**
- Modify: `src/snakeprune/_extract.py` (per-rule loop)
- Modify: `src/snakeprune/cli.py:152` (wording)
- Test: `tests/test_patterns.py`, `tests/test_cli.py`

- [ ] **Step 1: Write the tests**

Append to `tests/test_patterns.py`:

```python
def test_run_extractor_includes_named_log_entries(make_pipeline):
    # log: can carry several named entries; every one is rule-produced.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    log:\n"
        "        out = 'logs/{sample}.out',\n"
        "        err = 'logs/{sample}.err'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert "logs/{sample}.out" in specs[0].outputs
    assert "logs/{sample}.err" in specs[0].outputs


def test_run_extractor_includes_repeat_flagged_benchmark(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    benchmark: repeat('benchmarks/{sample}.tsv', 3)\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert "benchmarks/{sample}.tsv" in specs[0].outputs


def test_run_extractor_dedups_log_equal_to_output(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    log: 'results/{sample}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert specs[0].outputs == ["results/{sample}.txt"]


@pytest.mark.parametrize("directive", ["log", "benchmark"])
def test_run_extractor_callable_log_or_benchmark_fails_loudly(make_pipeline, directive):
    # Snakemake 9.x happens to reject callable log/benchmark during rule
    # registration, so the extractor's own guard is currently unreachable; the
    # contract pinned here is only "ExtractorError, never a pattern". Do not
    # assert on the message text.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        f"    {directive}: lambda w: 'x/{{sample}}.log'\n"
        "    shell: 'touch {output}'\n"
    )
    with pytest.raises(ExtractorError):
        run_extractor(pipeline)
```

- [ ] **Step 2: Run them**

Run: `python -m pytest tests/test_patterns.py -k "named_log or repeat_flagged or dedups or callable_log" -v`
Expected: PASS for all except possibly `dedups` (already PASS after Task 1's `dict.fromkeys`). If `repeat_flagged` fails, inspect `str(rule.benchmark)` — it should be the plain path; adjust nothing in the test unless Snakemake wraps it differently, in which case record what it emits in the test comment.

- [ ] **Step 3: Add the callable guard in the extractor**

In `_extract.py`, import `is_callable` alongside `is_flagged` and, before `raw_outputs = ...`, add:

```python
            for o in produced:
                if callable(o) or is_callable(o):
                    raise ValueError(
                        f"rule {rule.name}: an output/log/benchmark path is a "
                        f"function; snakeprune cannot derive a pattern from it"
                    )
```

- [ ] **Step 4: Wording**

`cli.py:152`: change `log(f"Loaded {len(patterns)} rule output pattern(s).")` to `log(f"Loaded {len(patterns)} rule path pattern(s) (output, log, benchmark).")`. Update any test asserting the old string (`grep -n "rule output pattern" tests/`) to the new one. Change the empty-rules refusal text `"Workflow loaded but produced 0 output patterns."` to `"Workflow loaded but produced 0 path patterns."` and update any test asserting `"0 output patterns"` (`grep -n "0 output patterns" tests/`) to `"0 path patterns"`.

- [ ] **Step 5: Run the suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit and push**

```bash
git add src/snakeprune/_extract.py src/snakeprune/cli.py tests/test_patterns.py tests/test_cli.py
git commit -m "chore(extract): guard callable paths, dedup, pin named-log and repeat() coverage

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

---

### Task 9: README limitations and test-coverage doc

**Files:**
- Modify: `README.md` ("Safety and limitations" → "Limitations"; "How it works" step 3)
- Modify: `docs/test-coverage.md` (counts and new guarantees)

- [ ] **Step 1: README limitations**

Under "### Limitations", add these bullets (before "Module-imported rules"):

```markdown
- **Undeclared sidecar files are orphans.** Only paths a rule *declares* (`output`, `log`, `benchmark`) are live. Index and companion files that tools write alongside a declared output — `.bai`, `.csi`, `.tbi`, `.fai`, `.dict`, `Rplots.pdf`, `samtools sort` temporaries, `*.log` written by the tool rather than by `log:` — are reported as orphans. Declare them (`multiext`, `touch()`, an extra `output:` entry) or protect them with `--ignore "**/*.bai"`.
- **`directory()` outputs.** Everything beneath a `directory()` output is treated as live, because Snakemake owns the whole tree. `snakeprune` cannot see stale files *inside* such a directory.
- **Do not run while the workflow is running.** In-flight jobs write temporary and partial files that match no pattern; wait for `snakemake` to finish (and for no `.snakemake/locks` to exist) before pruning.
```

In "### Built-in refusals" add:

```markdown
- **Unparseable rule paths.** If a rule path contains brace syntax `snakeprune` cannot read, it exits 4 rather than guess — an unparsed pattern would mark every file of that rule as an orphan.
- **Absolute output paths outside the project.** A rule writing to an absolute path that is not under the workflow directory can never match a scanned file; the pattern is dropped with a warning (shown even under `--quiet`). Absolute paths *under* the workflow directory are relativised and work normally.
```

Update "How it works" step 3 to read:

```markdown
3. For each path a rule produces (`output`, `log`, and `benchmark`), parse `{wildcard}` and `{wildcard,regex}` placeholders exactly as Snakemake does — Snakemake has already inlined rule-local and global `wildcard_constraints` into each string, and the inline annotation wins; unconstrained wildcards default to `[^/]+` (or Snakemake's `.+` under `--permissive-wildcards`). `directory()` outputs additionally match everything beneath them. The combined alternation regex is built once for the whole scan.
```

- [ ] **Step 2: Update `docs/test-coverage.md`**

Run `python -m pytest -q --co | tail -1` to get the new total and per-file counts (`python -m pytest -q --co tests/test_patterns.py | tail -1` etc.). Replace the counts in the intro paragraph. Under "Part 1 — Being correct about what is an orphan", add a short paragraph:

```markdown
### Constraint syntax, `directory()` outputs and absolute paths

Three classes of input used to produce *silent* false orphans and are now pinned:
brace-quantified constraints (`{chrom,[0-9]{1,2}}`, at inline, rule and global
level — `test_cli_scan_brace_quantifier_constraint_is_live`), files inside a
`directory()` output including `.snakemake_timestamp`
(`test_cli_scan_files_inside_directory_output_are_live`), and outputs built from
an absolute path under the project (`test_cli_scan_absolute_output_under_project_is_live`).
Anything brace-shaped the parser cannot read now raises instead of being
escaped into a never-matching literal (`test_unparseable_brace_raises_pattern_error`,
`test_cli_scan_unparseable_pattern_exits_4`).
```

Under the refusals/deletion part, add one sentence each for the trash-inside-results refusal and the exit-5 partial-deletion contract, naming the tests from Tasks 5 and 6.

- [ ] **Step 3: Sanity-check the docs render**

Run: `grep -c "" README.md docs/test-coverage.md` (just confirms the files are non-empty and were saved); then `python -m pytest -q` one final time.
Expected: all PASS.

- [ ] **Step 4: Commit and push**

```bash
git add README.md docs/test-coverage.md
git commit -m "docs: document sidecar files, directory() handling, exit codes and new guards

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```
