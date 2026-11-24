import os
import re

import pytest

from snakeprune.patterns import rule_literal_prefixes
from snakeprune.walker import _matches_any_glob, iter_results_files


def test_iter_results_files_basic(make_results):
    results = make_results(["a.txt", "sub/b.txt", "sub/deeper/c.csv"])
    rels = sorted(rel for _, rel in iter_results_files(results))
    assert rels == ["a.txt", "sub/b.txt", "sub/deeper/c.csv"]


def test_iter_results_files_skips_symlinks_by_default(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    rels = [rel for _, rel in iter_results_files(results)]
    assert "a.txt" in rels
    assert "link.txt" not in rels


def test_iter_results_files_follow_symlinks_when_requested(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    rels = [rel for _, rel in iter_results_files(results, follow_symlinks=True)]
    assert "link.txt" in rels


def test_iter_results_files_ignore_globs(make_results):
    results = make_results(["a.txt", "notes/manual.md", "x.log"])
    rels = sorted(
        rel for _, rel in iter_results_files(results, ignore_globs=["notes/**", "*.log"])
    )
    assert rels == ["a.txt"]


def test_iter_results_files_directories_not_returned(make_results):
    results = make_results(["sub/a.txt"])
    for full_path, _ in iter_results_files(results):
        assert os.path.isfile(full_path)


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
    assert stats == {**{"skipped_symlinked_dirs": 0, "excluded_dirs": 1}, "glob_hits": set()}


def test_iter_results_files_stats_counts_skipped_symlinked_dirs(make_results, tmp_path):
    results = make_results(["a.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    stats: dict = {}
    rels = [rel for _, rel in iter_results_files(results, stats=stats)]
    assert "a.txt" in rels
    # The symlinked directory itself was skipped, and its contents were not visited.
    assert "link_dir/x.txt" not in rels
    assert stats == {**{"skipped_symlinked_dirs": 1, "excluded_dirs": 0}, "glob_hits": set()}


def test_iter_results_files_stats_zero_when_no_dir_symlinks(make_results):
    results = make_results(["a.txt", "sub/b.txt"])
    stats: dict = {}
    list(iter_results_files(results, stats=stats))
    assert stats == {**{"skipped_symlinked_dirs": 0, "excluded_dirs": 0}, "glob_hits": set()}


def test_iter_results_files_stats_omitted_does_not_error(make_results, tmp_path):
    # File-symlinks and dir-symlinks both present; default kwarg path must still work.
    results = make_results(["a.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    (results / "link_file").symlink_to(external / "x.txt")
    rels = [rel for _, rel in iter_results_files(results)]
    assert rels == ["a.txt"]



def test_attribute_orphan_uses_precomputed_prefixes(monkeypatch):
    import snakeprune.walker as walker
    pats = [("a", re.compile(r"^results/a/(?P<s>[^/]+)\.txt$")),
            ("b", re.compile(r"^results/b/(?P<s>[^/]+)\.txt$"))]
    prefixes = rule_literal_prefixes(pats)
    assert prefixes == [("a", "results/a/"), ("b", "results/b/")]

    def boom(_):
        raise AssertionError("prefix recomputed per orphan")

    monkeypatch.setattr(walker, "extract_literal_prefix", boom)
    assert walker.attribute_orphan_to_rule("results/b/old.csv", pats, prefixes) == "b"


@pytest.mark.parametrize("rel,glob,expected", [
    ("sample.bam.bai", "**/*.bai", True),
    ("a/b/sample.bam.bai", "**/*.bai", True),
    ("a/b.txt", "a/**/b.txt", True),
    ("a/x/y/b.txt", "a/**/b.txt", True),
    ("sample.bam", "**/*.bai", False),
    ("b/a/b.txt", "a/**/b.txt", False),
    ("sub/x.txt", "sub/**", True),
    ("a/b/c.log", "*.log", True),   # `*` still crosses `/`
])
def test_ignore_glob_semantics(rel, glob, expected):
    assert _matches_any_glob(rel, [glob]) is expected


def test_iter_results_files_counts_skipped_symlinked_dirs_when_following(make_results, tmp_path):
    results = make_results(["a.txt"])
    external = tmp_path / "ext"
    external.mkdir()
    (external / "f.txt").write_text("f")
    (results / "big").symlink_to(external)
    target = tmp_path / "target.txt"
    target.write_text("t")
    (results / "link_file").symlink_to(target)
    stats: dict = {}
    rels = [rel for _, rel in iter_results_files(results, follow_symlinks=True, stats=stats)]
    assert stats["skipped_symlinked_dirs"] == 1
    assert "link_file" in rels
    assert not any(r == "big" or r.startswith("big/") for r in rels)


def test_ignore_glob_repeated_double_stars_collapse():
    # Previously expanded into 2**n variants and hung.
    glob = "**/" * 60 + "b.txt"
    assert _matches_any_glob("a/x/b.txt", [glob]) is True
    assert _matches_any_glob("a/x/c.txt", [glob]) is False


def test_ignore_glob_with_too_many_double_stars_is_rejected():
    assert _matches_any_glob("a/b/c/d.txt", ["**/b/**/c/**/d.txt"]) is True
    with pytest.raises(ValueError, match="more than 3"):
        _matches_any_glob("a/b/c/d/e.txt", ["**/b/**/c/**/d/**/e.txt"])


def test_ignore_glob_subdir_suffix_matches_the_prefix_itself():
    assert _matches_any_glob("sub", ["sub/**"]) is True
    assert _matches_any_glob("subway/x", ["sub/**"]) is False
