import re

import pytest

from snakeprune.patterns import wildcard_pattern_to_regex
from snakeprune.report import (
    AmbiguityExplainer,
    DirectorySummary,
    OrphanSelection,
    OrphanSpool,
)


def _spool(entries):
    spool = OrphanSpool()
    for full, rel, ambiguous, likely in entries:
        spool.add(full, rel, ambiguous, likely)
    spool.finish()
    return spool


def test_spool_round_trips_awkward_names_exactly():
    # A newline, a decomposed accent and an undecodable byte (surrogate-escaped
    # as os.fsdecode produces it) must all come back unchanged and unnormalised.
    names = ["a\nb.txt", "café/x.txt", "bad\udcff.txt", "tab\there.txt"]
    spool = _spool([(f"/r/results/{n}", n, False, None) for n in names])
    got = [(str(o.path), o.rel) for o in spool.iter_orphans()]
    assert got == [(f"/r/results/{n}", n) for n in names]


def test_spool_is_reiterable_and_filters_ambiguous():
    spool = _spool([
        ("/r/a.txt", "a.txt", False, "rule_a"),
        ("/r/s/b.txt", "s/b.txt", True, None),
        ("/r/c.txt", "c.txt", False, None),
    ])
    assert spool.count == 3 and spool.ambiguous_count == 1
    first = [o.rel for o in spool.iter_orphans()]
    second = [o.rel for o in spool.iter_orphans()]
    assert first == second == ["a.txt", "s/b.txt", "c.txt"]
    sel = OrphanSelection(spool, include_ambiguous=False)
    assert len(sel) == 2
    assert [o.rel for o in sel] == ["a.txt", "c.txt"]
    assert [o.rel for o in sel] == ["a.txt", "c.txt"]
    assert next(iter(spool.iter_orphans())).likely_rule == "rule_a"
    assert [o.ambiguous for o in spool.iter_orphans()] == [False, True, False]
    assert len(OrphanSelection(spool, include_ambiguous=True)) == 3


def test_spool_truncation_raises():
    spool = _spool([("/r/a.txt", "a.txt", False, None)])
    spool._file.truncate(5)
    with pytest.raises(OSError):
        list(spool.iter_orphans())


def test_directory_summary_groups_by_depth():
    s = DirectorySummary(depth=2)
    s.add("top.txt", deletable=True, size=1)
    s.add("a/b/c/d.txt", deletable=True, size=10)
    s.add("a/b/e.txt", deletable=False, size=100)
    s.add("a/x.txt", deletable=True, size=1000)
    lines = s.render()
    assert lines[0].split() == ["deletable", "ambiguous", "size", "directory"]
    rows = {line.split()[-1]: line.split()[:2] for line in lines[1:-1]}
    assert rows == {"a/b/": ["1", "1"], "a/": ["1", "0"], "./": ["1", "0"]}
    # Largest group first; total last.
    assert lines[1].endswith("a/b/")
    assert lines[-1].split()[:2] == ["3", "1"] and lines[-1].endswith("(total)")


def test_directory_summary_rejects_zero_depth():
    with pytest.raises(ValueError):
        DirectorySummary(depth=0)


def _permissive(name, path):
    return (name, re.compile(wildcard_pattern_to_regex(path, {}, default=".+")))


def test_explainer_lists_every_unconstrained_wildcard_in_the_rule():
    # Two adjacent unconstrained wildcards: either may absorb the extra level,
    # so both must be named, not just the one the greedy match picked.
    rule = _permissive("fit", "results/{inc,include|exclude}/{pc}/{eqtl}/{opt,lbfgsb}/x.csv")
    ex = AmbiguityExplainer([rule])
    rel = "include/uncentered/0_01/0_01/lbfgsb/x.csv"
    ex.add(rel, ["results/" + rel])
    ex.add(rel, ["results/" + rel])
    text = "\n".join(ex.render())
    assert "fit: 2 file(s); unconstrained: {pc} {eqtl}" in text
    assert "inc" not in text.split("unconstrained:")[1].splitlines()[0]
    assert "pc='uncentered/0_01'" in text
    assert "constrain every wildcard" in text


def test_explainer_uses_whichever_target_matches():
    ex = AmbiguityExplainer([_permissive("r", "out/{n}.txt")])
    ex.add("a/b.txt", ["results/a/b.txt", "out/a/b.txt"])
    assert "r: 1 file(s)" in "\n".join(ex.render())


def test_explainer_caps_rules_shown():
    pats = [_permissive(f"r{i}", f"d{i}/{{n}}.txt") for i in range(25)]
    ex = AmbiguityExplainer(pats)
    for i in range(25):
        ex.add("a/b.txt", [f"d{i}/a/b.txt"])
    text = "\n".join(ex.render())
    assert "... and 5 more rule(s) covering 5 file(s)." in text
