from snakeprune.walker import iter_results_files


def test_iter_results_files_basic(make_results):
    results = make_results(["a.txt", "sub/b.txt", "sub/deeper/c.csv"])
    paths = sorted(p.relative_to(results).as_posix() for p in iter_results_files(results))
    assert paths == ["a.txt", "sub/b.txt", "sub/deeper/c.csv"]


def test_iter_results_files_skips_symlinks_by_default(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    paths = [p.name for p in iter_results_files(results)]
    assert "a.txt" in paths
    assert "link.txt" not in paths


def test_iter_results_files_follow_symlinks_when_requested(make_results, tmp_path):
    results = make_results(["a.txt"])
    target = tmp_path / "outside.txt"
    target.write_text("x")
    link = results / "link.txt"
    link.symlink_to(target)
    paths = [p.name for p in iter_results_files(results, follow_symlinks=True)]
    assert "link.txt" in paths


def test_iter_results_files_ignore_globs(make_results):
    results = make_results(["a.txt", "notes/manual.md", "x.log"])
    paths = sorted(
        p.relative_to(results).as_posix()
        for p in iter_results_files(results, ignore_globs=["notes/**", "*.log"])
    )
    assert paths == ["a.txt"]


def test_iter_results_files_directories_not_returned(make_results):
    results = make_results(["sub/a.txt"])
    for p in iter_results_files(results):
        assert p.is_file()


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
