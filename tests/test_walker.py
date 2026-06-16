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
