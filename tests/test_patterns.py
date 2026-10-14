# tests/test_patterns.py
import pytest

def test_make_pipeline_writes_snakefile(make_pipeline):
    pipeline = make_pipeline("rule all:\n    input: 'results/x.txt'\n")
    assert (pipeline / "Snakefile").read_text().startswith("rule all:")


def test_make_results_creates_files(make_results):
    results = make_results(["a/b.txt", "c/d/e.csv"])
    assert (results / "a" / "b.txt").exists()
    assert (results / "c" / "d" / "e.csv").exists()


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


import re as _re_module


def test_constraint_with_alternation_compiles_and_matches_each_alternative():
    """Constraint body 'x|y' must match 'x' and 'y' but not 'xy' when compiled."""
    regex_str = wildcard_pattern_to_regex("{a}.txt", constraints={"a": "x|y"})
    pat = _re_module.compile(regex_str)
    assert pat.match("x.txt")
    assert pat.match("y.txt")
    assert not pat.match("xy.txt")


def test_repeated_wildcard_uses_backreference():
    """A wildcard appearing twice in a pattern produces a named group on first
    occurrence and a backreference on subsequent occurrences, preserving
    Snakemake's implicit-equality semantics."""
    regex_str = wildcard_pattern_to_regex("results/{x}/{x}.txt", constraints={})
    pat = _re_module.compile(regex_str)
    assert pat.match("results/abc/abc.txt")
    assert not pat.match("results/abc/xyz.txt")


def test_repeated_wildcard_with_constraint_uses_backreference():
    regex_str = wildcard_pattern_to_regex(
        "{n}/{n}.csv", constraints={"n": r"\d+"}
    )
    pat = _re_module.compile(regex_str)
    assert pat.match("123/123.csv")
    assert not pat.match("123/456.csv")
    assert not pat.match("abc/abc.csv")


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


def test_unclosed_brace_fails_fast():
    # An unclosed brace must raise promptly: the parser's constraint group
    # must not backtrack exponentially (a hang would mask the loud failure).
    import time
    from snakeprune.patterns import PatternError
    start = time.monotonic()
    with pytest.raises(PatternError):
        wildcard_pattern_to_regex("r/{x," + "a" * 40 + " {", constraints={})
    assert time.monotonic() - start < 1.0


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
    msg = str(exc.value)
    assert str(pipeline / "Snakefile") in msg
    assert str(pipeline / "workflow" / "Snakefile") in msg


from snakeprune.patterns import load_rule_specs, find_rule_patterns, RuleSpec


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

def test_load_rule_specs_multiple_rules(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/a.txt'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule b:\n"
        "    output: 'results/b.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    names = sorted(s.name for s in specs)
    assert names == ["a", "b"]


def test_load_rule_specs_module_imported_rules_visible(make_pipeline):
    """Rules imported via `module ... use rule * from foo` must appear in
    the parent workflow's rule list, with their output patterns intact."""
    pipeline = make_pipeline(
        "module foo:\n"
        "    snakefile: 'foo.smk'\n"
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
    assert any(s.name == "a" for s in specs), [s.name for s in specs]
    a = next(s for s in specs if s.name == "a")
    assert a.outputs == ["results/{n}.txt"]


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
    # Snakemake does NOT inline global constraints into module-imported rules,
    # so the path carries no annotation; the fallback dict supplies it.
    assert a.outputs == ["subdir/results/{n}.txt"], a.outputs
    assert a.constraints.get("n") == "[0-9]{2}"

def test_load_rule_specs_does_not_pollute_cwd(make_pipeline, tmp_path, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    monkeypatch.chdir(tmp_path)
    # Snapshot dir contents before
    before = set(tmp_path.iterdir())
    load_rule_specs(pipeline)
    after = set(tmp_path.iterdir())
    new_entries = after - before
    # Should not have created a .snakemake/ in CWD
    assert not any(p.name == ".snakemake" for p in new_entries)


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


from snakeprune.patterns import combine_rule_patterns


def _compile(p):
    return _re_module.compile(p)


def test_combine_rule_patterns_empty_returns_none():
    assert combine_rule_patterns([]) is None


def test_combine_rule_patterns_matches_any_input_pattern():
    patterns = [
        ("a", _compile(r"^results/a/(?P<n>[^/]+)\.txt$")),
        ("b", _compile(r"^results/b/(?P<n>[^/]+)\.csv$")),
    ]
    combined = combine_rule_patterns(patterns)
    assert combined is not None
    assert combined.match("results/a/1.txt")
    assert combined.match("results/b/2.csv")
    assert not combined.match("results/c/3.txt")


def test_combine_rule_patterns_handles_shared_wildcard_names():
    # Both rules use {sample}; naive | -join would error on duplicate group name.
    patterns = [
        ("align", _compile(r"^results/align/(?P<sample>[^/]+)\.bam$")),
        ("call", _compile(r"^results/call/(?P<sample>[^/]+)\.vcf$")),
    ]
    combined = combine_rule_patterns(patterns)
    assert combined is not None
    assert combined.match("results/align/s1.bam")
    assert combined.match("results/call/s1.vcf")


def test_combine_rule_patterns_preserves_repeated_wildcard_equality():
    # `results/{sample}/{sample}.bam` requires the two {sample} slots to match;
    # the rewrite must preserve that backreference.
    repeated = wildcard_pattern_to_regex(
        "results/{sample}/{sample}.bam", constraints={}
    )
    patterns = [("dedup", _compile(repeated))]
    combined = combine_rule_patterns(patterns)
    assert combined is not None
    assert combined.match("results/abc/abc.bam")
    assert not combined.match("results/abc/xyz.bam")


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


def test_run_extractor_workflow_layout_uses_project_root_as_workdir(tmp_path):
    # Standard Snakemake layout: the Snakefile lives under workflow/, and the
    # workflow reads a project-root-relative file (resources/...) at load time.
    # Snakemake's convention is that the working directory is the PARENT of
    # workflow/, so resources/... resolves against the project root. When
    # snakeprune is pointed straight at workflow/, it must load with that same
    # workdir; otherwise the top-level read fails with FileNotFoundError and the
    # scan aborts (exit 2).
    root = tmp_path / "proj"
    (root / "workflow").mkdir(parents=True)
    (root / "resources").mkdir()
    (root / "resources" / "samples.txt").write_text("s1\n")
    (root / "workflow" / "Snakefile").write_text(
        "with open('resources/samples.txt') as _f:\n"
        "    _f.read()\n"
        "rule a:\n"
        "    output: 'results/a.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(root / "workflow")
    assert [s.name for s in specs] == ["a"]


def test_run_extractor_includes_log_paths(make_pipeline):
    # A rule's log: files are produced by the rule just like its outputs, so they
    # must be emitted as live patterns. Otherwise a log file under the scanned
    # directory is flagged as an orphan (and --delete would remove it).
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    log: 'logs/{sample}.log'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    outputs = specs[0].outputs
    assert "results/{sample}.txt" in outputs
    assert "logs/{sample}.log" in outputs


def test_run_extractor_includes_benchmark_path(make_pipeline):
    # benchmark: files are rule-produced too and carry the same deletion risk.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    benchmark: 'benchmarks/{sample}.tsv'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = run_extractor(pipeline)
    assert "benchmarks/{sample}.tsv" in specs[0].outputs


def test_run_extractor_callable_output_fails_loudly(make_pipeline):
    # A function/callable output is rejected by Snakemake itself at load time
    # ("Only input files can be specified as functions"), so it can never reach
    # str(o) and be silently turned into a bogus orphan-matching pattern.
    # snakeprune must surface that rejection as a loud ExtractorError (CLI exit
    # 4), never produce an empty/garbage pattern set.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: lambda wildcards: 'results/x.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    with pytest.raises(ExtractorError) as exc:
        run_extractor(pipeline)
    msg = str(exc.value)
    assert "Extractor failed" in msg
    assert "function" in msg.lower()
