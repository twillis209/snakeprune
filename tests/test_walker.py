import os

from snakeprune.walker import iter_results_files


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
    assert stats == {"skipped_symlinked_dirs": 0, "excluded_dirs": 1}


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
    assert stats == {"skipped_symlinked_dirs": 1, "excluded_dirs": 0}


def test_iter_results_files_stats_zero_when_no_dir_symlinks(make_results):
    results = make_results(["a.txt", "sub/b.txt"])
    stats: dict = {}
    list(iter_results_files(results, stats=stats))
    assert stats == {"skipped_symlinked_dirs": 0, "excluded_dirs": 0}


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


def test_find_orphans_sets_rel_on_orphan_file(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete/3.txt"])
    orphans = find_orphans(pipeline, results)
    assert len(orphans) == 1
    assert orphans[0].rel == "obsolete/3.txt"
