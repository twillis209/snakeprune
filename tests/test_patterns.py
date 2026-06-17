# tests/test_patterns.py
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
    msg = str(exc.value)
    assert str(pipeline / "Snakefile") in msg
    assert str(pipeline / "workflow" / "Snakefile") in msg


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


def test_load_rule_specs_inline_constraint_captured(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    assert specs[0].outputs == ["results/{x}.txt"]
    assert specs[0].constraints.get("x") == "[0-9]+"


def test_load_rule_specs_inline_constraint_overridden_by_rule_local(make_pipeline):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[0-9]+}.txt'\n"
        "    wildcard_constraints:\n"
        "        x = r'[ab]+'\n"
        "    shell: 'touch {output}'\n"
    )
    specs = load_rule_specs(pipeline)
    # Rule-local declared constraint wins over inline annotation
    assert specs[0].constraints.get("x") == "[ab]+"


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
    every file under `subdir/results/` would look like an orphan."""
    pipeline = make_pipeline(
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
    assert a.outputs == ["subdir/results/{n}.txt"], a.outputs


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
