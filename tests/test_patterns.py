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
