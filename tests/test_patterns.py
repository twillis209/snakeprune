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
